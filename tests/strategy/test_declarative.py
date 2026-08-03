import pytest

from koval.strategy.base.declarative import DeclarativeStrategy
from koval.strategy.base.trade_setup import TradeSetup


class _AlwaysLong(DeclarativeStrategy):
    def should_long(self) -> bool:
        return True

    def go_long(self) -> TradeSetup:
        return TradeSetup(
            direction="long",
            entry_price=self.close,
            stop_loss=self.close * 0.95,
            entry_type="market",
            why_entry=["always long test stub"],
        )


def test_default_should_long_returns_false():
    class _Noop(DeclarativeStrategy):
        pass

    s = _Noop()
    assert s.should_long() is False


def test_default_should_short_returns_false():
    class _Noop(DeclarativeStrategy):
        pass

    s = _Noop()
    assert s.should_short() is False


def test_subclass_overrides_should_long():
    s = _AlwaysLong()
    assert s.should_long() is True


def test_go_long_returns_trade_setup():
    s = _AlwaysLong()
    s.close = 100.0
    setup = s.go_long()
    assert isinstance(setup, TradeSetup)
    assert setup.direction == "long"
    assert setup.entry_price == 100.0
    assert setup.stop_loss == pytest.approx(95.0)


def test_default_go_long_raises():
    class _Noop(DeclarativeStrategy):
        pass

    s = _Noop()
    with pytest.raises(NotImplementedError):
        s.go_long()


def test_default_go_short_raises():
    class _Noop(DeclarativeStrategy):
        pass

    s = _Noop()
    with pytest.raises(NotImplementedError):
        s.go_short()


def test_execute_filters_empty_returns_true():
    s = _AlwaysLong()
    assert s._execute_filters() is True


def test_execute_filters_all_pass():
    s = _AlwaysLong()
    s._filter_fns = [lambda: True, lambda: True]
    s.__class__.filters = lambda self: self._filter_fns
    assert s._execute_filters() is True


def test_execute_filters_one_fail_returns_false():
    s = _AlwaysLong()
    s._filter_fns = [lambda: True, lambda: False, lambda: True]
    s.__class__.filters = lambda self: self._filter_fns
    assert s._execute_filters() is False


def test_state_fields_have_defaults():
    s = _AlwaysLong()
    assert s.close == 0.0
    assert s.high == 0.0
    assert s.low == 0.0
    assert s.open == 0.0
    assert s.volume == 0.0
    assert s.bar_index == 0
    assert s.account_value == 0.0
    assert s.position_size == 0.0
    assert s.position_direction is None
    assert s.config == {}


def test_strategy_instances_do_not_share_mutable_config():
    first = _AlwaysLong()
    second = _AlwaysLong()

    first.config["symbol"] = "BTCUSDT"

    assert second.config == {}


def test_should_cancel_entry_default_false():
    s = _AlwaysLong()
    assert s.should_cancel_entry() is False


def test_on_bar_default_is_no_op():
    s = _AlwaysLong()
    assert s.on_bar() is None


def test_on_sl_update_default_none():
    s = _AlwaysLong()
    assert s.on_sl_update(1) is None


def test_on_tp_update_default_none():
    s = _AlwaysLong()
    assert s.on_tp_update(1) is None


def test_metadata_returns_dict_with_required_keys():
    meta = _AlwaysLong.metadata()
    assert isinstance(meta, dict)
    for key in ("name", "description", "author", "tags", "version"):
        assert key in meta, f"metadata() missing key: '{key}'"


def test_metadata_tags_is_list():
    meta = _AlwaysLong.metadata()
    assert isinstance(meta["tags"], list)


def test_metadata_version_is_string():
    meta = _AlwaysLong.metadata()
    assert isinstance(meta["version"], str)


def test_declarative_strategy_has_htf_history_fields():
    from koval.strategy.base.declarative import DeclarativeStrategy

    s = DeclarativeStrategy()
    assert s.htf_closes is None
    assert s.htf_highs is None
    assert s.htf_lows is None
    assert s.htf_opens is None
    assert s.htf_volumes is None


def test_no_backtrader_import_in_declarative():
    import importlib
    import sys

    mod_name = "koval.strategy.base.declarative"
    if mod_name in sys.modules:
        mod = sys.modules[mod_name]
    else:
        mod = importlib.import_module(mod_name)
    for name, obj in vars(mod).items():
        assert not (
            hasattr(obj, "__module__")
            and str(getattr(obj, "__module__", "")).startswith("backtrader")
        ), (
            f"backtrader object '{name}' found in declarative.py — BT must stay in adapters/backtrader/"
        )
