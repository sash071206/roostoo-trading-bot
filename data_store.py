"""Builds and persists fixed-interval price bars from polled ticker snapshots."""
from __future__ import annotations

import csv
import logging
import os
from collections import defaultdict
from typing import Dict, List, Tuple

import requests

log = logging.getLogger(__name__)

Bar = Tuple[int, float]  # (bar_id, close) where bar_id = unix_seconds // bar_seconds


class BarStore:
    def __init__(self, path: str, bar_minutes: int, max_bars: int):
        self.path = path
        self.bar_minutes = bar_minutes
        self.bar_seconds = bar_minutes * 60
        self.max_bars = max_bars
        self.bars: Dict[str, List[Bar]] = defaultdict(list)
        self.current: Dict[str, Bar] = {}  # in-progress bar per pair

    # ---------- persistence ----------
    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        merged: Dict[str, Dict[int, float]] = defaultdict(dict)
        with open(self.path) as f:
            for row in csv.reader(f):
                if len(row) != 3 or row[0] == "bar_id":
                    continue
                merged[row[1]][int(row[0])] = float(row[2])
        for pair, d in merged.items():
            self.bars[pair] = sorted(d.items())[-self.max_bars:]
        log.info("Loaded bars for %d pairs from %s", len(self.bars), self.path)

    def save_all(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["bar_id", "pair", "close"])
            for pair, lst in self.bars.items():
                for bar_id, close in lst:
                    w.writerow([bar_id, pair, close])

    def _persist(self, pair: str, bar: Bar) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        new = not os.path.exists(self.path)
        with open(self.path, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["bar_id", "pair", "close"])
            w.writerow([bar[0], pair, bar[1]])

    # ---------- updates ----------
    def _append(self, pair: str, bar: Bar, persist: bool = True) -> None:
        lst = self.bars[pair]
        if lst and lst[-1][0] >= bar[0]:
            if lst[-1][0] == bar[0]:
                lst[-1] = bar
            return
        lst.append(bar)
        if len(lst) > self.max_bars:
            del lst[: len(lst) - self.max_bars]
        if persist:
            self._persist(pair, bar)

    def update(self, tickers: Dict[str, dict], now: float) -> None:
        """Feed one ticker snapshot (all pairs). Closes a bar when the interval rolls over."""
        bar_id = int(now // self.bar_seconds)
        for pair, t in tickers.items():
            px = t.get("LastPrice")
            if not px:
                continue
            cur = self.current.get(pair)
            if cur is not None and cur[0] != bar_id:
                self._append(pair, cur)
            self.current[pair] = (bar_id, float(px))

    def closes(self, pair: str) -> List[float]:
        """Completed bar closes plus the latest live price as the final point."""
        out = [c for _, c in self.bars.get(pair, [])]
        cur = self.current.get(pair)
        if cur is not None:
            if self.bars.get(pair) and self.bars[pair][-1][0] == cur[0]:
                out[-1] = cur[1]
            else:
                out.append(cur[1])
        return out

    # ---------- bootstrap ----------
    def bootstrap_from_binance(self, pairs: List[str], needed: int) -> None:
        """Fill missing history from Binance public klines (COIN/USD -> COINUSDT)."""
        changed = False
        for pair in pairs:
            if len(self.bars.get(pair, [])) >= needed:
                continue
            coin = pair.split("/")[0]
            hist = fetch_binance_closes(coin, self.bar_minutes, self.bar_seconds, min(needed + 20, 1000))
            if not hist:
                log.info("No bootstrap data for %s", pair)
                continue
            merged = dict(hist)
            merged.update(dict(self.bars.get(pair, [])))  # local bars take precedence
            self.bars[pair] = sorted(merged.items())[-self.max_bars:]
            changed = True
            log.info("Bootstrapped %s with %d bars", pair, len(hist))
        if changed:
            self.save_all()


def fetch_binance_closes(coin: str, bar_minutes: int, bar_seconds: int, limit: int) -> List[Bar]:
    symbol = f"{coin}USDT"
    for host in ("https://data-api.binance.vision", "https://api.binance.com"):
        try:
            r = requests.get(f"{host}/api/v3/klines",
                             params={"symbol": symbol, "interval": f"{bar_minutes}m", "limit": limit},
                             timeout=10)
            if r.status_code != 200:
                continue
            rows = r.json()
            # drop the last kline: it is still in progress
            return [(int(k[0]) // 1000 // bar_seconds, float(k[4])) for k in rows[:-1]]
        except (requests.RequestException, ValueError, IndexError):
            continue
    return []
