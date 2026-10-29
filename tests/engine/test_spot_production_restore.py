"""Recovery refuses incomplete archives and retains terminal single-use facts."""

from copy import deepcopy

import pytest

from koval.engine.run_identity import content_sha256
from tests.engine.test_spot_production import ProductionBroker, live
from tests.engine.test_spot_production_controls import MUTATIONS, Host, protected, residual


class ArchiveHost(Host):
    def __init__(self, bundle):
        super().__init__()
        self.expected = content_sha256(bundle)

    def verify_spot_restore(self, bundle):
        if content_sha256(bundle) != self.expected:
            raise ValueError("authoritative archive head mismatch")
        return self.expected


def recovered(bundle):
    broker = ProductionBroker()
    engine, broker, _ = live(broker=broker, host=ArchiveHost(bundle))
    return engine, broker


def test_every_production_record_has_identity_session_software_context():
    engine, broker, records = protected()
    assert records
    assert all(r["spot_context"] == broker.context for r in records)
    snapshot = engine.spot_state
    assert snapshot["archive_segments"][-1]["records"] == records
    assert snapshot["archive_segments"][-1]["checkpoint"] == snapshot["archive_checkpoint"]


def test_terminal_restore_retains_oco_and_never_mutates_after_stop_eof_exception():
    from koval.engine.live_feed import StopSignal

    engine, broker, _ = protected(Host())
    engine.finish_spot(disposition="custody_transferred", evidence_hash="evidence")
    bundle = engine.spot_state
    for ending in ("stop", "eof", "exception"):
        restored, rb = recovered(bundle)
        restored.restore_spot(bundle)
        rb.last_report = broker.last_report

        class Feed:
            def bars(self, stop, ending=ending):
                if ending == "exception":
                    raise RuntimeError("synthetic feed failure")
                if ending == "stop":
                    stop.set()
                return iter(())

        if ending == "exception":
            with pytest.raises(RuntimeError, match="synthetic feed"):
                restored.run(Feed(), StopSignal())
        else:
            restored.run(Feed(), StopSignal())
        restored.supervise_spot()
        assert restored.spot_account["entries_halted"]
        assert restored.spot_account["base_quantity"] == "2"
        assert restored.spot_account["protection"] is not None
        assert not [c for c in rb.calls if c[0] in MUTATIONS]
        assert restored.spot_state["execution"]["terminal"] == bundle["execution"]["terminal"]


@pytest.mark.parametrize("damage", ["tail", "hash", "context", "execution", "missing", "terminal"])
def test_restore_refuses_incomplete_mixed_or_modified_bundle_before_calls(damage):
    engine, _, _ = protected(Host())
    engine.finish_spot(disposition="custody_transferred", evidence_hash="evidence")
    bundle = engine.spot_state
    restored, broker = recovered(bundle)
    changed = deepcopy(bundle)
    if damage == "tail":
        changed["archive_segments"][-1]["records"].pop()
    elif damage == "hash":
        changed["archive_checkpoint"]["record_sha256"] = "changed"
    elif damage == "context":
        changed["execution"]["identity"]["environment"] = "testnet"
    elif damage == "execution":
        changed["execution"]["intents"] = []
    elif damage == "terminal":
        changed["execution"]["terminal"] = None
    else:
        changed.pop("archive_segments")
    before = list(broker.calls)
    with pytest.raises(ValueError):
        restored.restore_spot(changed)
    assert broker.calls == before


def test_archive_self_consistency_cannot_replace_independent_host_head_check():
    engine, _, _ = protected()
    bundle = engine.spot_state
    restored, broker = recovered(bundle)
    changed = deepcopy(bundle)
    changed["execution"]["halt_reason"] = "forged assembly"
    with pytest.raises(ValueError, match="archive"):
        restored.restore_spot(changed)
    assert not [c for c in broker.calls if c[0] in MUTATIONS]


def test_disposal_replay_after_restore_sends_nothing_and_preserves_receipt():
    engine, _, _, state_hash = residual()
    receipt = engine.retry_spot_close(attempt_id="second", expected_state_hash=state_hash)
    bundle = engine.spot_state
    restored, broker = recovered(bundle)
    restored.restore_spot(bundle)
    assert restored.retry_spot_close(attempt_id="second", expected_state_hash=state_hash) == receipt
    assert not [c for c in broker.calls if c[0] in MUTATIONS]


def test_testnet_archive_cannot_default_to_production():
    from koval.engine.live_engine import LiveEngine, LiveEngineConfig
    from tests.engine.spot_fixtures import ScriptedSpotBroker
    from tests.engine.test_spot_testnet import spot_graph

    testnet = LiveEngine(
        spot_graph(),
        LiveEngineConfig("BTCUSDT", "1m", 1000, market="spot"),
        broker=ScriptedSpotBroker(),
        on_record=lambda r: None,
    )
    production, broker, _ = live(host=Host())
    with pytest.raises(ValueError):
        production.restore_spot(testnet.spot_state)
    assert not [c for c in broker.calls if c[0] in MUTATIONS]


def test_json_roundtrip_recovery_preserves_durable_receipts():
    import json

    engine, _, _ = protected(Host())
    engine.finish_spot(disposition="custody_transferred", evidence_hash="evidence")
    bundle = json.loads(json.dumps(engine.spot_state))
    restored, _ = recovered(bundle)
    restored.restore_spot(bundle)
    again = restored.spot_state
    second, _ = recovered(again)
    second.restore_spot(again)
    assert second.spot_account["entries_halted"]
    assert second.spot_state["execution"]["terminal"] == bundle["execution"]["terminal"]


def test_failed_restore_inhibits_engine_instead_of_resuming_empty_session():
    engine, _, _ = protected()
    bundle = engine.spot_state
    restored, _ = recovered(bundle)
    changed = deepcopy(bundle)
    changed["execution"]["identity"]["account_id"] = "foreign"
    with pytest.raises(ValueError):
        restored.restore_spot(changed)
    assert restored.spot_account["entries_halted"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("close_id", None),
        ("close_attempted", False),
        ("entry_id", None),
        ("order_acks", []),
    ],
)
def test_restore_verifies_derived_attempt_facts_against_unchanged_durable_journal(field, value):
    engine, _, _, _ = residual()
    bundle = engine.spot_state

    class HeadOnlyHost(Host):
        def verify_spot_restore(self, proposed):
            assert proposed["archive_segments"] == bundle["archive_segments"]
            assert proposed["archive_checkpoint"] == bundle["archive_checkpoint"]
            return content_sha256(proposed)

    restored, broker, _ = live(host=HeadOnlyHost())
    changed = deepcopy(bundle)
    changed["execution"][field] = value
    before = list(broker.calls)
    with pytest.raises(ValueError, match="archive"):
        restored.restore_spot(changed)
    assert broker.calls == before
