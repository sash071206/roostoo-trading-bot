"""Portfolio management: turns strategy targets into orders, with stops and a drawdown breaker."""
from __future__ import annotations

import csv
import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

from roostoo_client import RoostooError
from strategy import Signal, compute_signal, is_risk_on, stop_pct, target_weights

log = logging.getLogger(__name__)


def spot_wallet(bal: dict) -> dict:
    """The live API returns balances under 'SpotWallet'; the docs show 'Wallet'. Accept both."""
    return bal.get("SpotWallet") or bal.get("Wallet") or {}


def floor_to(x: float, decimals: int) -> float:
    """Round down to the exchange's amount step. Never rounds up, so a sell can't exceed holdings."""
    f = 10 ** decimals
    return math.floor(x * f * (1 - 1e-12)) / f


class Trader:
    def __init__(self, client, store, cfg):
        self.client = client
        self.store = store
        self.cfg = cfg
        self.pair_info: Dict[str, dict] = {}
        self.state = self._load_state()

    # ---------- state ----------
    def _load_state(self) -> dict:
        default = {"peak_equity": 0.0, "breaker_until": 0.0, "entry_peaks": {},
                   "entry_time": {}, "cooldown_until": {}, "last_fill_day": ""}
        if os.path.exists(self.cfg.STATE_FILE):
            with open(self.cfg.STATE_FILE) as f:
                default.update(json.load(f))
        return default

    def save_state(self) -> None:
        os.makedirs(os.path.dirname(self.cfg.STATE_FILE) or ".", exist_ok=True)
        with open(self.cfg.STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=2)

    # ---------- market metadata ----------
    def refresh_exchange_info(self) -> None:
        info = self.client.exchange_info()
        pairs = info.get("TradePairs", {})
        self.pair_info = {p: v for p, v in pairs.items() if v.get("CanTrade", True) and p.endswith("/USD")}
        log.info("Exchange info: %d tradable USD pairs", len(self.pair_info))

    def universe(self, tickers: Dict[str, dict]) -> list:
        pairs = [p for p in self.pair_info if p in tickers]
        pairs.sort(key=lambda p: tickers[p].get("UnitTradeValue", 0) or 0, reverse=True)
        uni = pairs[: self.cfg.UNIVERSE_SIZE]
        if self.cfg.REGIME_PAIR in self.pair_info and self.cfg.REGIME_PAIR not in uni:
            uni.append(self.cfg.REGIME_PAIR)
        return uni

    # ---------- portfolio ----------
    def portfolio(self, tickers: Dict[str, dict]) -> Tuple[float, Dict[str, Tuple[float, float, float]], float]:
        """Returns (usd_free, holdings{pair: (total_qty, free_qty, price)}, equity)."""
        bal = self.client.balance()
        if not bal.get("Success"):
            raise RoostooError(f"balance failed: {bal.get('ErrMsg')}")
        wallet = spot_wallet(bal)
        usd = wallet.get("USD", {})
        usd_free = float(usd.get("Free", 0) or 0)
        equity = usd_free + float(usd.get("Lock", 0) or 0)
        holdings: Dict[str, Tuple[float, float, float]] = {}
        for coin, v in wallet.items():
            if coin == "USD":
                continue
            free = float(v.get("Free", 0) or 0)
            qty = free + float(v.get("Lock", 0) or 0)
            pair = f"{coin}/USD"
            px = tickers.get(pair, {}).get("LastPrice")
            if qty <= 0 or not px:
                continue
            equity += qty * float(px)
            if qty * float(px) >= self.cfg.DUST_USD:
                holdings[pair] = (qty, free, float(px))
        return usd_free, holdings, equity

    # ---------- main decision step ----------
    def rebalance(self, tickers: Dict[str, dict], now: float) -> None:
        cfg, st = self.cfg, self.state
        self._now = now
        usd_free, holdings, equity = self.portfolio(tickers)
        if equity <= 0:
            log.error("Equity is 0 -- wallet empty or balance format unexpected; skipping rebalance")
            return

        # Drawdown circuit breaker
        if st["breaker_until"] and now >= st["breaker_until"]:
            log.info("Breaker cooldown over; resuming with peak reset to %.2f", equity)
            st["breaker_until"] = 0.0
            st["peak_equity"] = equity
        st["peak_equity"] = max(st["peak_equity"], equity)
        drawdown = 1 - equity / st["peak_equity"] if st["peak_equity"] > 0 else 0.0
        breaker = now < st["breaker_until"]
        if not breaker and drawdown >= cfg.MAX_DRAWDOWN:
            st["breaker_until"] = now + cfg.BREAKER_COOLDOWN_HOURS * 3600
            breaker = True
            log.warning("Drawdown %.2f%% hit breaker: flattening for %dh",
                        drawdown * 100, cfg.BREAKER_COOLDOWN_HOURS)

        # Signals
        uni = set(self.universe(tickers)) | set(holdings)
        signals: Dict[str, Signal] = {}
        for pair in uni:
            sig = compute_signal(pair, self.store.closes(pair), cfg, held=pair in holdings)
            if sig is not None:
                signals[pair] = sig
        regime = signals.get(cfg.REGIME_PAIR)
        if regime is None:
            log.info("Warming up: not enough %s history yet (%d bars)",
                     cfg.REGIME_PAIR, len(self.store.closes(cfg.REGIME_PAIR)))
            self._log_equity(now, equity, usd_free, holdings, drawdown, "warmup")
            return

        # Trailing stops
        stopped = set()
        for pair, (_, _, px) in holdings.items():
            peak = max(float(st["entry_peaks"].get(pair, px)), px)
            st["entry_peaks"][pair] = peak
            sp = stop_pct(signals.get(pair), cfg)
            if px <= peak * (1 - sp):
                stopped.add(pair)
                st["cooldown_until"][pair] = now + cfg.STOP_COOLDOWN_HOURS * 3600
                log.info("Trailing stop %s: price %.6g <= peak %.6g x (1-%.1f%%)", pair, px, peak, sp * 100)

        # Minimum holding period: young positions whose trend is intact keep their slot
        locked = set()
        for pair in holdings:
            entered = float(st["entry_time"].setdefault(pair, now))
            sig = signals.get(pair)
            if (now - entered < cfg.MIN_HOLD_HOURS * 3600 and pair not in stopped
                    and sig is not None and sig.eligible):
                locked.add(pair)

        # Targets
        if breaker:
            targets: Dict[str, float] = {}
        else:
            usable = {p: s for p, s in signals.items()
                      if p in self.pair_info and p not in stopped
                      and now >= float(st["cooldown_until"].get(p, 0))}
            targets = target_weights(usable, regime, cfg, locked=locked)
        risk_on = is_risk_on(regime)
        log.info("Equity %.2f | DD %.2f%% | BTC regime %s | targets %s",
                 equity, drawdown * 100, "ON" if risk_on else "OFF",
                 {p: round(w, 3) for p, w in targets.items()} or "cash")

        # Orders: sells first to free cash, then buys
        current_w = {p: q * px / equity for p, (q, _, px) in holdings.items()}
        sells, buys = [], []
        for pair in set(current_w) | set(targets):
            tw, cw = targets.get(pair, 0.0), current_w.get(pair, 0.0)
            # Held coins are only resized when far from target (fee control); new ones use the
            # smaller threshold.
            band = cfg.HELD_DRIFT_TOLERANCE if (pair in holdings and tw > 0) else cfg.MIN_TRADE_FRACTION
            if tw == 0.0 and pair in holdings:
                reason = "stop" if pair in stopped else ("breaker" if breaker else "exit")
                sells.append((pair, None, holdings[pair][1], reason))
            elif tw - cw <= -band:
                sells.append((pair, (cw - tw) * equity, None, "trim"))
            elif tw - cw >= band:
                buys.append((pair, (tw - cw) * equity, "entry" if pair not in holdings else "add"))

        for pair, notional, qty, reason in sells:
            self._execute(pair, "SELL", tickers, notional=notional, qty=qty, reason=reason,
                          full_exit=qty is not None)
        if buys:
            usd_free, _, _ = self.portfolio(tickers)
            budget = usd_free * 0.995  # leave room for the 0.1% fee
            for pair, notional, reason in sorted(buys, key=lambda b: -b[1]):
                amt = min(notional, budget)
                if amt <= 0:
                    break
                if self._execute(pair, "BUY", tickers, notional=amt, reason=reason):
                    budget -= amt

        self._keepalive(tickers, now, equity, signals, breaker)
        usd_free, holdings, equity = self.portfolio(tickers)
        self._log_equity(now, equity, usd_free, holdings, drawdown, "breaker" if breaker else "ok")
        self.save_state()

    # ---------- daily activity safeguard ----------
    def _local_day(self, now: float) -> Tuple[str, int]:
        local = datetime.fromtimestamp(now, tz=timezone.utc) + timedelta(hours=self.cfg.KEEPALIVE_TZ_OFFSET_HOURS)
        return local.strftime("%Y-%m-%d"), local.hour

    def _keepalive(self, tickers, now, equity, signals, breaker) -> None:
        cfg = self.cfg
        if not cfg.KEEPALIVE_ENABLED or breaker:
            return
        day, hour = self._local_day(now)
        if self.state["last_fill_day"] == day or hour < cfg.KEEPALIVE_LOCAL_HOUR:
            return
        ranked = sorted(signals.values(), key=lambda s: s.score, reverse=True)
        pair = next((s.pair for s in ranked if s.pair in self.pair_info), cfg.REGIME_PAIR)
        usd_free, _, _ = self.portfolio(tickers)
        amt = min(equity * cfg.KEEPALIVE_FRACTION, usd_free * 0.995)
        log.info("Keepalive: no fills yet on %s, placing small probe in %s", day, pair)
        self._execute(pair, "BUY", tickers, notional=amt, reason="keepalive")

    # ---------- execution ----------
    def _execute(self, pair: str, side: str, tickers: Dict[str, dict], notional: Optional[float] = None,
                 qty: Optional[float] = None, reason: str = "", full_exit: bool = False) -> bool:
        info = self.pair_info.get(pair)
        t = tickers.get(pair)
        if not info or not t:
            log.warning("Skip %s %s: no exchange info or price", side, pair)
            return False
        px = float((t.get("MinAsk") if side == "BUY" else t.get("MaxBid")) or t["LastPrice"])
        prec = int(info.get("AmountPrecision", 6))
        if qty is None:
            qty = notional / px
            if side == "SELL":
                held = self._free_qty(pair)
                qty = min(qty, held)
        qty = floor_to(qty, prec)
        if qty <= 0 or qty * px <= float(info.get("MiniOrder", 1.0)):
            log.info("Skip %s %s: size too small (qty=%s)", side, pair, qty)
            return False

        qty_str = f"{qty:.{prec}f}"
        try:
            res = self.client.place_order(pair, side, qty_str)
        except RoostooError as e:
            res = {"Success": False, "ErrMsg": str(e)}
        ok = bool(res.get("Success"))
        d = res.get("OrderDetail", {}) or {}
        fill_px = d.get("FilledAverPrice") or px
        self._log_trade(pair, side, qty_str, fill_px, reason, ok, d.get("OrderID", ""),
                        d.get("Status", ""), res.get("ErrMsg", ""))
        if ok:
            log.info("%s %s %s @ %.6g (%s)", side, qty_str, pair, float(fill_px), reason)
            day, _ = self._local_day(getattr(self, "_now", None) or time.time())
            self.state["last_fill_day"] = day
            if side == "BUY":
                self.state["entry_peaks"].setdefault(pair, float(fill_px))
                self.state["entry_time"].setdefault(pair, getattr(self, "_now", None) or time.time())
            elif full_exit:
                self.state["entry_peaks"].pop(pair, None)
                self.state["entry_time"].pop(pair, None)
        else:
            log.error("Order failed %s %s %s: %s", side, qty_str, pair, res.get("ErrMsg"))
        return ok

    def _free_qty(self, pair: str) -> float:
        coin = pair.split("/")[0]
        bal = self.client.balance()
        return float(spot_wallet(bal).get(coin, {}).get("Free", 0) or 0)

    # ---------- logs ----------
    def _append_csv(self, path: str, header: list, row: list) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        new = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(header)
            w.writerow(row)

    def _log_trade(self, pair, side, qty, price, reason, ok, order_id, status, err) -> None:
        self._append_csv(self.cfg.TRADES_FILE,
                         ["time_utc", "pair", "side", "quantity", "price", "notional", "reason",
                          "success", "order_id", "status", "error"],
                         [datetime.now(timezone.utc).isoformat(timespec="seconds"), pair, side, qty,
                          price, round(float(qty) * float(price), 2), reason, ok, order_id, status, err])

    def _log_equity(self, now, equity, usd, holdings, drawdown, note) -> None:
        exposure = 1 - usd / equity if equity > 0 else 0.0
        self._append_csv(self.cfg.EQUITY_FILE,
                         ["time_utc", "equity", "usd_free", "exposure", "drawdown", "positions", "note"],
                         [datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="seconds"),
                          round(equity, 2), round(usd, 2), round(exposure, 4), round(drawdown, 4),
                          ";".join(sorted(holdings)), note])
