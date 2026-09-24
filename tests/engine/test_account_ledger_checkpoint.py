"""Hash-verified ledger checkpoints for durable paper recovery."""

import pytest

from koval.engine.account_ledger import AccountLedger


def test_ledger_checkpoint_round_trip_preserves_entries_and_balance():
    ledger = AccountLedger(1_000)
    ledger.record(timestamp_ms=1, kind="commission", amount=-1, reference_id="entry")
    ledger.record(timestamp_ms=2, kind="funding", amount=2, reference_id="funding-2")

    restored = AccountLedger.from_checkpoint(ledger.checkpoint())

    assert restored.entries == ledger.entries
    assert restored.balance == 1_001
    assert restored.checkpoint() == ledger.checkpoint()


def test_ledger_checkpoint_rejects_changed_content_and_broken_sequence():
    ledger = AccountLedger(1_000)
    ledger.record(timestamp_ms=1, kind="commission", amount=-1)
    checkpoint = ledger.checkpoint()
    checkpoint["entries"][0]["amount"] = -2

    with pytest.raises(ValueError, match="ledger checkpoint hash mismatch"):
        AccountLedger.from_checkpoint(checkpoint)

    checkpoint = ledger.checkpoint()
    checkpoint["entries"][0]["sequence"] = 2
    from koval.engine.run_identity import content_sha256

    checkpoint["sha256"] = content_sha256(
        {key: value for key, value in checkpoint.items() if key != "sha256"}
    )
    with pytest.raises(ValueError, match="ledger checkpoint sequence"):
        AccountLedger.from_checkpoint(checkpoint)
