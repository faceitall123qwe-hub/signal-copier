from decimal import Decimal

from exchange.sizing import (
    SymbolFilters,
    compute_size,
    floor_to_step,
    max_safe_leverage,
    round_price,
)


def mk_filters(**kw):
    base = dict(
        qty_step=Decimal("0.001"),
        min_order_qty=Decimal("0.001"),
        min_notional=Decimal("5"),
        tick_size=Decimal("0.1"),
        max_leverage=100,
        maintenance_margin_rate=Decimal("0.005"),
    )
    base.update(kw)
    return SymbolFilters(**base)


def test_floor_and_round():
    assert floor_to_step(Decimal("50.7"), Decimal("0.1")) == Decimal("50.7")
    assert floor_to_step(Decimal("50.79"), Decimal("0.1")) == Decimal("50.7")
    assert round_price(Decimal("100.34"), Decimal("0.5")) == Decimal("100.5")
    assert round_price(Decimal("100.20"), Decimal("0.5")) == Decimal("100.0")


def test_risk_based_qty():
    r = compute_size(
        equity=Decimal("10000"), entry=Decimal("100"), sl=Decimal("98"),
        filters=mk_filters(), risk_pct=0.01, signal_leverage=None,
        default_leverage=10, max_leverage=10, fee_buffer=0.005,
    )
    assert r.ok
    # R=100, stop_dist=2 -> qty=50
    assert r.qty == Decimal("50")
    assert r.notional == Decimal("5000")


def test_missing_sl_skips():
    r = compute_size(
        equity=Decimal("10000"), entry=Decimal("100"), sl=None,
        filters=mk_filters(), risk_pct=0.01, signal_leverage=None,
        default_leverage=10, max_leverage=10, fee_buffer=0.005,
        fallback_stop_pct=None,
    )
    assert not r.ok and "no stop" in r.reason


def test_fallback_stop_used():
    r = compute_size(
        equity=Decimal("10000"), entry=Decimal("100"), sl=None,
        filters=mk_filters(), risk_pct=0.01, signal_leverage=None,
        default_leverage=10, max_leverage=10, fee_buffer=0.005,
        fallback_stop_pct=0.02,
    )
    assert r.ok


def test_stop_too_wide_skips():
    r = compute_size(
        equity=Decimal("100"), entry=Decimal("50000"), sl=Decimal("25000"),
        filters=mk_filters(), risk_pct=0.01, signal_leverage=None,
        default_leverage=10, max_leverage=10, fee_buffer=0.005,
    )
    assert not r.ok and "too wide" in r.reason


def test_min_notional_skips():
    r = compute_size(
        equity=Decimal("10000"), entry=Decimal("100"), sl=Decimal("98"),
        filters=mk_filters(min_notional=Decimal("100000")), risk_pct=0.01,
        signal_leverage=None, default_leverage=10, max_leverage=10,
        fee_buffer=0.005,
    )
    assert not r.ok and "notional" in r.reason


def test_leverage_capped_so_liq_beyond_sl():
    # wide-ish stop forces L_safe below default leverage
    entry, sl = Decimal("100"), Decimal("90")
    r = compute_size(
        equity=Decimal("100000"), entry=entry, sl=sl, filters=mk_filters(),
        risk_pct=0.01, signal_leverage=20, default_leverage=10,
        max_leverage=50, fee_buffer=0.005,
    )
    assert r.ok
    L = r.leverage
    stop_frac = abs(entry - sl) / entry
    # stop must trigger strictly before liquidation
    assert stop_frac < (Decimal(1) / Decimal(L)) - (Decimal("0.005") + Decimal("0.005"))
    # and it must be the safety cap that bound it
    assert L == max_safe_leverage(entry, sl, Decimal("0.005"), Decimal("0.005"))


def test_leverage_respects_max():
    r = compute_size(
        equity=Decimal("100000"), entry=Decimal("100"), sl=Decimal("99.9"),
        filters=mk_filters(), risk_pct=0.01, signal_leverage=None,
        default_leverage=10, max_leverage=10, fee_buffer=0.005,
    )
    assert r.ok and r.leverage <= 10


def test_insufficient_margin_skip_vs_reduce():
    common = dict(
        equity=Decimal("10000"), entry=Decimal("100"), sl=Decimal("98"),
        filters=mk_filters(), risk_pct=0.01, signal_leverage=None,
        default_leverage=10, max_leverage=10, fee_buffer=0.005,
        available_margin=Decimal("100"),
    )
    skip = compute_size(**common, on_insufficient_margin="skip")
    assert not skip.ok and "insufficient" in skip.reason
    reduce = compute_size(**common, on_insufficient_margin="reduce")
    assert reduce.ok and reduce.required_margin <= Decimal("100")
