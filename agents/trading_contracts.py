"""Shared action and execution-window contracts.

Keep cross-module protocol values here so a new decision action cannot be
tradable in one component while silently ignored by another.
"""
from __future__ import annotations


# R07 (2026-10-01): 单一权威 — 以下集合全部由 trading_actions 的决策词表派生,
# 不再手写并行定义 (之前 SELL_ALL / REDUCE_RISK 在 enum 里可下单, 这里却没有).
from trading_actions import Action as _A, decision_strings as _ds

BUY_ACTIONS = _ds(_A.BUY, _A.PROBE, _A.ADD)      # BUY WATCH_BUY WATCH_BUY_PROBE PROBE ADD
SELL_ACTIONS = _ds(_A.EXIT)                       # SELL SELL_ALL EXIT
REDUCE_ACTIONS = _ds(_A.REDUCE)                   # REDUCE REDUCE_RISK
ORDER_ACTIONS = BUY_ACTIONS | SELL_ACTIONS | REDUCE_ACTIONS

PROBE_ONLY_ACTIONS = _ds(_A.PROBE)               # WATCH_BUY_PROBE PROBE
CRISIS_PROBE_TARGET_VOL = 0.05
NON_EXECUTING_BULLISH_ACTIONS = _ds(_A.WATCH)    # WATCH_BUY_LONG_HOLD WATCH (不下单)
BULLISH_SIGNAL_ACTIONS = BUY_ACTIONS | NON_EXECUTING_BULLISH_ACTIONS
BEARISH_SIGNAL_ACTIONS = SELL_ACTIONS | REDUCE_ACTIONS | frozenset({"CAUTION"})

# pre-open intentionally refreshes the universe but does not place orders.
TRADE_WINDOWS = frozenset({"pre-market", "post-open", "midday", "pre-close"})

# Thresholds are authored on a 10-point scale. TECHNICAL_ONLY decisions use a
# 5-point scale, so every consumer must call confidence_min() before comparing.
WINDOW_CONF_MIN_10 = {
    "pre-market": 7,
    "post-open": 6,
    "midday": 6,
    "pre-close": 6,
}


def extended_chase_signals(market: dict | None) -> list[str]:
    """Return independent signs that a BUY would be chasing an extended move."""
    market = market or {}
    checks = (
        ("daily_surge", "pct_chg", 5.0),
        ("five_day_extension", "cum_5d_pct", 15.0),
        ("cci_overbought", "cci_20", 120.0),
        ("upper_band_extension", "bb_pct", 0.90),
        ("far_above_ma20", "dist_from_ma20_pct", 12.0),
    )
    triggered: list[str] = []
    for label, field, threshold in checks:
        try:
            if float(market.get(field)) >= threshold:
                triggered.append(label)
        except (TypeError, ValueError):
            continue
    return triggered


def confidence_min(window: str | None, scale: int = 10) -> float:
    """Return the window threshold converted to the active confidence scale."""
    safe_scale = scale if scale > 0 else 10
    raw = WINDOW_CONF_MIN_10.get(str(window), 6)
    return raw * safe_scale / 10


def confidence_multiplier(confidence: float, scale: int = 10, *, probe: bool = False) -> float:
    """Map confidence to the shared position-size multiplier."""
    if probe:
        return 0.30
    safe_scale = scale if scale > 0 else 10
    ratio = confidence / safe_scale
    if ratio < 0.40:
        return 0.30
    if ratio < 0.60:
        return 0.65
    if ratio < 0.80:
        return 1.00
    return 1.0 + (ratio - 0.80) * 2.0
