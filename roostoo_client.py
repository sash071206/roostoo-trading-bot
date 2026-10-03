"""Thin client for the Roostoo mock exchange REST API (signed HMAC-SHA256 requests)."""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any, Dict, Optional

import requests

log = logging.getLogger(__name__)


class RoostooError(Exception):
    pass


class RoostooClient:
    def __init__(self, api_key: str, secret_key: str, base_url: str, timeout: float = 10.0):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.time_offset_ms = 0  # server_time - local_time

    # ---------- helpers ----------
    def _timestamp(self) -> str:
        return str(int(time.time() * 1000) + self.time_offset_ms)

    def sign(self, params: Dict[str, str]) -> tuple:
        """Return (totalParams, signature): params sorted by key, joined k=v with '&'."""
        total = "&".join(f"{k}={params[k]}" for k in sorted(params))
        sig = hmac.new(self.secret_key.encode("utf-8"), total.encode("utf-8"), hashlib.sha256).hexdigest()
        return total, sig

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
                 auth: str = "none", retries: int = 3) -> Dict[str, Any]:
        params = {k: str(v) for k, v in (params or {}).items()}
        if auth in ("ts", "signed"):
            params["timestamp"] = self._timestamp()

        headers: Dict[str, str] = {}
        body = None
        if auth == "signed":
            total, sig = self.sign(params)
            headers["RST-API-KEY"] = self.api_key
            headers["MSG-SIGNATURE"] = sig
            if method == "POST":
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                body = total

        url = self.base_url + path
        # Never auto-retry POSTs: a retried order could execute twice.
        attempts = retries if method == "GET" else 1
        last_err: Optional[Exception] = None
        for i in range(attempts):
            try:
                if method == "GET":
                    r = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
                else:
                    r = self.session.post(url, data=body if body is not None else params,
                                          headers=headers, timeout=self.timeout)
                if r.status_code != 200:
                    raise RoostooError(f"HTTP {r.status_code}: {r.text[:300]}")
                return r.json()
            except (requests.RequestException, ValueError, RoostooError) as e:
                last_err = e
                log.warning("%s %s attempt %d/%d failed: %s", method, path, i + 1, attempts, e)
                if i < attempts - 1:
                    time.sleep(2 ** i)
        raise RoostooError(f"{method} {path} failed: {last_err}")

    # ---------- public ----------
    def server_time(self) -> int:
        return int(self._request("GET", "/v3/serverTime")["ServerTime"])

    def sync_time(self) -> int:
        local = int(time.time() * 1000)
        self.time_offset_ms = self.server_time() - local
        return self.time_offset_ms

    def exchange_info(self) -> Dict[str, Any]:
        return self._request("GET", "/v3/exchangeInfo")

    def ticker(self, pair: Optional[str] = None) -> Dict[str, Any]:
        params = {"pair": pair} if pair else {}
        return self._request("GET", "/v3/ticker", params, auth="ts")

    # ---------- signed ----------
    def balance(self) -> Dict[str, Any]:
        return self._request("GET", "/v3/balance", auth="signed")

    def pending_count(self) -> Dict[str, Any]:
        return self._request("GET", "/v3/pending_count", auth="signed")

    def place_order(self, pair: str, side: str, quantity: str,
                    order_type: str = "MARKET", price: Optional[str] = None) -> Dict[str, Any]:
        params = {"pair": pair, "side": side.upper(), "type": order_type.upper(), "quantity": quantity}
        if order_type.upper() == "LIMIT":
            if price is None:
                raise ValueError("LIMIT orders need a price")
            params["price"] = price
        return self._request("POST", "/v3/place_order", params, auth="signed")

    def query_order(self, order_id: Optional[str] = None, pair: Optional[str] = None,
                    pending_only: Optional[bool] = None) -> Dict[str, Any]:
        params: Dict[str, str] = {}
        if order_id:
            params["order_id"] = str(order_id)
        elif pair:
            params["pair"] = pair
            if pending_only is not None:
                params["pending_only"] = "TRUE" if pending_only else "FALSE"
        return self._request("POST", "/v3/query_order", params, auth="signed")

    def cancel_order(self, order_id: Optional[str] = None, pair: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, str] = {}
        if order_id:
            params["order_id"] = str(order_id)
        elif pair:
            params["pair"] = pair
        return self._request("POST", "/v3/cancel_order", params, auth="signed")
