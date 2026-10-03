"""Entry point: polls prices every minute and rebalances every hour. Run inside tmux."""
from __future__ import annotations

import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

import config as cfg
from data_store import BarStore
from paper_client import PaperClient
from roostoo_client import RoostooClient, RoostooError
from strategy import min_bars_needed
from trader import Trader, spot_wallet


def setup_logging() -> None:
    os.makedirs(cfg.LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(os.path.join(cfg.LOG_DIR, "bot.log"), maxBytes=5_000_000, backupCount=5)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


def build_client():
    live = RoostooClient(cfg.API_KEY, cfg.SECRET_KEY, cfg.BASE_URL)
    if cfg.DRY_RUN:
        return PaperClient(live, cfg.PAPER_STARTING_USD, wallet_file=cfg.PAPER_WALLET_FILE)
    return live


def main() -> None:
    setup_logging()
    log = logging.getLogger("bot")
    log.info("Starting bot | mode=%s | base_url=%s", "PAPER" if cfg.DRY_RUN else "LIVE", cfg.BASE_URL)

    if not cfg.DRY_RUN and not (cfg.API_KEY and cfg.SECRET_KEY):
        log.error("ROOSTOO_API_KEY / ROOSTOO_SECRET_KEY missing in .env (or set DRY_RUN=1)")
        sys.exit(1)

    client = build_client()
    try:
        log.info("Clock offset vs server: %d ms", client.sync_time())
    except RoostooError as e:
        log.warning("Time sync failed (%s); using local clock", e)

    store = BarStore(cfg.BARS_FILE, cfg.BAR_MINUTES, cfg.MAX_BARS_PER_PAIR)
    store.load()
    trader = Trader(client, store, cfg)
    trader.refresh_exchange_info()

    bal = client.balance()
    if not bal.get("Success"):
        log.error("Balance check failed: %s -- check API keys / permissions", bal.get("ErrMsg"))
        sys.exit(1)
    wallet = spot_wallet(bal)
    log.info("Authenticated. Wallet: %s",
             {k: v for k, v in wallet.items() if (v.get("Free") or v.get("Lock"))})
    if not wallet:
        log.warning("Balance response has no wallet entries: %s", bal)

    first = client.ticker()
    if first.get("Success"):
        tickers = first.get("Data", {})
        store.update(tickers, time.time())
        if cfg.BOOTSTRAP_FROM_BINANCE:
            store.bootstrap_from_binance(trader.universe(tickers), min_bars_needed(cfg))

    period = cfg.REBALANCE_MINUTES * 60
    next_rebalance = time.time()  # rebalance right after startup
    next_info = time.time() + cfg.EXCHANGE_INFO_REFRESH_HOURS * 3600

    try:
        while True:
            loop_start = time.time()
            try:
                res = client.ticker()
                if res.get("Success"):
                    tickers = res.get("Data", {})
                    store.update(tickers, loop_start)
                    if loop_start >= next_info:
                        trader.refresh_exchange_info()
                        client.sync_time()
                        next_info = loop_start + cfg.EXCHANGE_INFO_REFRESH_HOURS * 3600
                    if loop_start >= next_rebalance:
                        trader.rebalance(tickers, loop_start)
                        next_rebalance = (loop_start // period + 1) * period + 30
                else:
                    log.warning("Ticker failed: %s", res.get("ErrMsg"))
            except Exception:  # keep the bot alive; every failure is logged
                log.exception("Loop error")
            time.sleep(max(1.0, cfg.POLL_SECONDS - (time.time() - loop_start)))
    except KeyboardInterrupt:
        log.info("Stopped by user")
    finally:
        trader.save_state()


if __name__ == "__main__":
    main()
