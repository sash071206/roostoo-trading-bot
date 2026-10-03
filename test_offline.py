"""Offline tests: signature check against the Roostoo docs example and a simulated-market run.

Run with:  python3 -m tests.test_offline
"""
import math
import os
import random
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as cfg  # noqa: E402
from data_store import BarStore  # noqa: E402
from paper_client import PaperClient  # noqa: E402
from roostoo_client import RoostooClient  # noqa: E402
from trader import Trader  # noqa: E402


def test_signature():
    c = RoostooClient("USEAPIKEYASMYID",
                      "S1XP1e3UZj6A7H5fATj0jNhqPxxdSJYdInClVN65XAbvqqMKjVHjA7PZj4W12oep", "http://x")
    total, sig = c.sign({"pair": "BNB/USD", "quantity": "2000", "side": "BUY",
                         "timestamp": "1580774512000", "type": "MARKET"})
    assert total == "pair=BNB/USD&quantity=2000&side=BUY&timestamp=1580774512000&type=MARKET"
    assert sig == "20b7fd5550b67b3bf0c1684ed0f04885261db8fdabd38611e9e6af23c19b7fff", sig
    print("signature OK")


class FakeMarket:
    """Random-walk market with a few trending and a few falling coins."""

    def __init__(self, seed=7):
        self.rng = random.Random(seed)
        self.drift = {"BTC/USD": 0.0004, "ETH/USD": 0.0006, "SOL/USD": 0.0009,
                      "DOGE/USD": -0.0008, "XRP/USD": -0.0003, "ADA/USD": 0.0}
        self.px = {"BTC/USD": 60000, "ETH/USD": 3000, "SOL/USD": 150,
                   "DOGE/USD": 0.15, "XRP/USD": 0.6, "ADA/USD": 0.45}

    def step(self):
        for p in self.px:
            self.px[p] *= math.exp(self.drift[p] + self.rng.gauss(0, 0.004))

    def sync_time(self):
        return 0

    def exchange_info(self):
        return {"IsRunning": True, "TradePairs": {
            p: {"Coin": p.split("/")[0], "CanTrade": True, "PricePrecision": 4,
                "AmountPrecision": 2 if self.px[p] < 10 else 5, "MiniOrder": 1.0} for p in self.px}}

    def ticker(self, pair=None):
        data = {p: {"LastPrice": v, "MaxBid": v * 0.9998, "MinAsk": v * 1.0002,
                    "UnitTradeValue": 1e6 * (i + 1)} for i, (p, v) in enumerate(self.px.items())}
        return {"Success": True, "Data": data}


def test_simulation():
    tmp = tempfile.mkdtemp()
    try:
        for name in ("STATE_FILE", "TRADES_FILE", "EQUITY_FILE", "BARS_FILE"):
            setattr(cfg, name, os.path.join(tmp, os.path.basename(getattr(cfg, name))))
        market = FakeMarket()
        client = PaperClient(market, 100_000.0)
        store = BarStore(cfg.BARS_FILE, cfg.BAR_MINUTES, cfg.MAX_BARS_PER_PAIR)
        trader = Trader(client, store, cfg)
        trader.refresh_exchange_info()

        t0 = 1_760_000_000.0
        bar = cfg.BAR_MINUTES * 60
        for i in range(24 * 4 * 6):  # 6 simulated days of 15-minute bars
            market.step()
            now = t0 + i * bar
            tick = client.ticker()["Data"]
            store.update(tick, now)
            if i % 4 == 0:  # hourly
                trader.rebalance(tick, now)

        _, holdings, equity = trader.portfolio(client.ticker()["Data"])
        with open(cfg.TRADES_FILE) as f:
            n_trades = sum(1 for _ in f) - 1
        print(f"simulated equity {equity:,.2f}, trades {n_trades}, holdings {sorted(holdings)}")
        assert n_trades > 0
        assert equity > 0
        assert client.wallet["USD"] >= -1e-6
        assert all(q >= -1e-9 for q in client.wallet.values())
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    test_signature()
    test_simulation()
    print("all tests passed")
