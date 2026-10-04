"""Volatility-adjusted trend-following with a BTC regime filter.

Pure functions only: no API calls, so the logic is easy to test and review.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import pstdev
from typing import Dict, Iterable, List, Optional


@dataclass
class Signal:
    pair: str
    price: float
    ema_fast: float
    ema_slow: float
    momentum: float      # return over MOM_LOOKBACK bars
    vol: float           # std-dev of per-bar log returns
    score: float         # risk-adjusted momentum
    eligible: bool


def ema(values: List[float], span: int) -> float:
    k = 2.0 / (span + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def min_bars_needed(cfg) -> int:
    return max(cfg.SLOW_EMA, cfg.MOM_LOOKBACK, cfg.VOL_LOOKBACK) + 2


def compute_signal(pair: str, closes: List[float], cfg, held: bool) -> Optional[Signal]:
    if len(closes) < min_bars_needed(cfg):
        return None
    window = closes[-cfg.SLOW_EMA * 3:]
    ef = ema(window, cfg.FAST_EMA)
    es = ema(window, cfg.SLOW_EMA)
    price = closes[-1]
    mom = price / closes[-1 - cfg.MOM_LOOKBACK] - 1.0
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - cfg.VOL_LOOKBACK, len(closes))]
    vol = pstdev(rets) or 1e-9
    score = mom / (vol * math.sqrt(cfg.MOM_LOOKBACK))

    if held:
        # Looser condition to stay in a position than to enter one (hysteresis cuts churn):
        # stay while the 6h EMA is above the 24h EMA, i.e. exit on a trend reversal, not on
        # every price dip below the 24h EMA. The trailing stop handles sharp drops.
        eligible = ef > es
        score += cfg.HOLD_BONUS
    else:
        eligible = price > es and ef > es and mom > 0
    return Signal(pair, price, ef, es, mom, vol, score, eligible)


def stop_pct(sig: Optional[Signal], cfg) -> float:
    if sig is None:
        return cfg.STOP_MAX
    bars_per_day = 24 * 60 / cfg.BAR_MINUTES
    daily_vol = sig.vol * math.sqrt(bars_per_day)
    return min(cfg.STOP_MAX, max(cfg.STOP_MIN, cfg.STOP_VOL_MULT * daily_vol))


def is_risk_on(regime: Optional[Signal]) -> bool:
    """BTC regime: risk-on while BTC's 6h EMA is above its 24h EMA (smoother than price vs EMA,
    so exposure does not flip between 90% and 30% on every small wobble)."""
    return regime is not None and regime.ema_fast > regime.ema_slow


def target_weights(signals: Dict[str, Signal], regime: Optional[Signal], cfg,
                   locked: Iterable[str] = ()) -> Dict[str, float]:
    """Pick the top-scoring eligible coins and size them by inverse volatility.

    `locked` pairs (recently entered, trend still intact) keep their slot ahead of new
    candidates, so the bot does not pay round-trip fees to swap between similar coins.
    """
    gross = cfg.MAX_GROSS_EXPOSURE if is_risk_on(regime) else cfg.RISK_OFF_EXPOSURE

    eligible = sorted((s for s in signals.values() if s.eligible), key=lambda s: s.score, reverse=True)
    locked = set(locked)
    chosen = [s for s in eligible if s.pair in locked][: cfg.MAX_POSITIONS]
    chosen += [s for s in eligible if s.pair not in locked][: cfg.MAX_POSITIONS - len(chosen)]
    if not chosen:
        return {}
    inv = {s.pair: 1.0 / s.vol for s in chosen}
    total = sum(inv.values())
    return {p: min(cfg.MAX_WEIGHT_PER_COIN, gross * w / total) for p, w in inv.items()}
