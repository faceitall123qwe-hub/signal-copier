"""Risk-based position sizing for linear (USDT) perpetuals.

qty is derived purely from risk-to-SL; leverage never enters qty. Leverage only
affects required margin and the liquidation price, so we cap leverage to keep the
liquidation price beyond the stop loss.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Optional


@dataclass
class SymbolFilters:
    qty_step: Decimal
    min_order_qty: Decimal
    min_notional: Decimal
    tick_size: Decimal
    max_leverage: int
    maintenance_margin_rate: Decimal  # e.g. 0.005


@dataclass
class SizingResult:
    ok: bool
    qty: Decimal = Decimal(0)
    leverage: int = 0
    notional: Decimal = Decimal(0)
    required_margin: Decimal = Decimal(0)
    reason: str = ""


def _d(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def round_price(price, tick: Decimal) -> Decimal:
    price = _d(price)
    if tick <= 0:
        return price
    return (price / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick


def max_safe_leverage(
    entry: Decimal, sl: Decimal, mmr: Decimal, fee_buffer: Decimal
) -> int:
    """Largest integer L such that SL triggers before liquidation.

    Require stop_dist/entry < (1/L) - (mmr + fee_buffer)
      => 1/L > stop_dist/entry + mmr + fee_buffer
      => L   < 1 / (stop_dist/entry + mmr + fee_buffer)
    """
    stop_frac = abs(entry - sl) / entry
    denom = stop_frac + mmr + fee_buffer
    if denom <= 0:
        return 1
    raw = Decimal(1) / denom
    L = int(raw.to_integral_value(rounding=ROUND_DOWN))
    return max(1, L)


def compute_size(
    *,
    equity: Decimal | float,
    entry: Decimal | float,
    sl: Optional[Decimal | float],
    filters: SymbolFilters,
    risk_pct: float,
    signal_leverage: Optional[int],
    default_leverage: int,
    max_leverage: int,
    fee_buffer: float,
    fallback_stop_pct: Optional[float] = None,
    available_margin: Optional[Decimal | float] = None,
    on_insufficient_margin: str = "skip",
) -> SizingResult:
    equity = _d(equity)
    entry = _d(entry)
    mmr = filters.maintenance_margin_rate
    fee_buf = _d(fee_buffer)

    if sl is None:
        if fallback_stop_pct is None:
            return SizingResult(False, reason="no stop loss in signal and no fallback")
        sl_d = entry * (Decimal(1) - _d(fallback_stop_pct)) if True else None
        # direction-agnostic fallback distance
        sl = entry * (Decimal(1) - _d(fallback_stop_pct))
    sl = _d(sl)

    stop_dist = abs(entry - sl)
    if stop_dist <= 0:
        return SizingResult(False, reason="stop distance is zero")

    R = equity * _d(risk_pct)
    raw_qty = R / stop_dist
    qty = floor_to_step(raw_qty, filters.qty_step)

    if qty < filters.min_order_qty:
        return SizingResult(
            False,
            reason=(
                f"stop too wide for min size: risk qty {raw_qty:f} < minOrderQty "
                f"{filters.min_order_qty:f}"
            ),
        )

    notional = qty * entry
    if notional < filters.min_notional:
        return SizingResult(
            False,
            reason=f"notional {notional:f} < minNotional {filters.min_notional:f}",
        )

    # leverage selection
    L_safe = max_safe_leverage(entry, sl, mmr, fee_buf)
    candidates = [L_safe, max_leverage, filters.max_leverage]
    candidates.append(signal_leverage if signal_leverage else default_leverage)
    leverage = max(1, min(c for c in candidates if c and c > 0))

    required_margin = notional / Decimal(leverage)

    if available_margin is not None:
        avail = _d(available_margin)
        if required_margin > avail:
            if on_insufficient_margin == "reduce":
                # reduce qty so margin fits, re-floor to step
                max_notional = avail * Decimal(leverage)
                qty = floor_to_step(max_notional / entry, filters.qty_step)
                if qty < filters.min_order_qty or qty * entry < filters.min_notional:
                    return SizingResult(
                        False, reason="insufficient margin even after reduce"
                    )
                notional = qty * entry
                required_margin = notional / Decimal(leverage)
            else:
                return SizingResult(
                    False,
                    reason=(
                        f"insufficient margin: need {required_margin:f}, "
                        f"have {avail:f}"
                    ),
                )

    return SizingResult(
        ok=True,
        qty=qty,
        leverage=leverage,
        notional=notional,
        required_margin=required_margin,
        reason="ok",
    )
