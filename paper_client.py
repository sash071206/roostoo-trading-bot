"""Paper-trading client: real Roostoo prices, simulated wallet and fills (DRY_RUN=1)."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional


class PaperClient:
    def __init__(self, market, starting_usd: float = 100_000.0, fee: float = 0.001,
                 wallet_file: Optional[str] = None):
        self.market = market          # anything with exchange_info() / ticker() / sync_time()
        self.fee = fee
        self.wallet_file = wallet_file
        self.wallet: Dict[str, float] = {"USD": starting_usd}
        self.order_id = 0
        self._last: Dict[str, Dict[str, float]] = {}
        if wallet_file and os.path.exists(wallet_file):
            with open(wallet_file) as f:
                saved = json.load(f)
            self.wallet = {k: float(v) for k, v in saved.get("wallet", {}).items()}
            self.order_id = int(saved.get("order_id", 0))

    def _save(self) -> None:
        if self.wallet_file:
            os.makedirs(os.path.dirname(self.wallet_file) or ".", exist_ok=True)
            with open(self.wallet_file, "w") as f:
                json.dump({"wallet": self.wallet, "order_id": self.order_id}, f)

    def sync_time(self) -> int:
        return self.market.sync_time() if hasattr(self.market, "sync_time") else 0

    def exchange_info(self) -> Dict[str, Any]:
        return self.market.exchange_info()

    def ticker(self, pair: Optional[str] = None) -> Dict[str, Any]:
        data = self.market.ticker(pair)
        if data.get("Success"):
            self._last.update(data.get("Data", {}))
        return data

    def balance(self) -> Dict[str, Any]:
        return {"Success": True, "ErrMsg": "",
                "Wallet": {c: {"Free": q, "Lock": 0.0} for c, q in self.wallet.items()}}

    def place_order(self, pair: str, side: str, quantity: str,
                    order_type: str = "MARKET", price: Optional[str] = None) -> Dict[str, Any]:
        t = self._last.get(pair)
        if t is None:
            return {"Success": False, "ErrMsg": "no price for pair"}
        coin = pair.split("/")[0]
        qty = float(quantity)
        usd = self.wallet.get("USD", 0.0)
        if side.upper() == "BUY":
            px = float(t.get("MinAsk") or t["LastPrice"])
            cost = qty * px
            fee = cost * self.fee
            if cost + fee > usd + 1e-9:
                return {"Success": False, "ErrMsg": "insufficient balance"}
            self.wallet["USD"] = usd - cost - fee
            self.wallet[coin] = self.wallet.get(coin, 0.0) + qty
        else:
            px = float(t.get("MaxBid") or t["LastPrice"])
            if qty > self.wallet.get(coin, 0.0) + 1e-12:
                return {"Success": False, "ErrMsg": "insufficient balance"}
            proceeds = qty * px
            fee = proceeds * self.fee
            self.wallet[coin] = self.wallet.get(coin, 0.0) - qty
            self.wallet["USD"] = usd + proceeds - fee
        self.order_id += 1
        self._save()
        return {"Success": True, "ErrMsg": "", "OrderDetail": {
            "Pair": pair, "OrderID": self.order_id, "Status": "FILLED", "Role": "TAKER",
            "Side": side.upper(), "Type": "MARKET", "Quantity": qty, "FilledQuantity": qty,
            "FilledAverPrice": px, "CommissionChargeValue": fee}}
