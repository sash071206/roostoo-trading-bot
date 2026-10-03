# Roostoo Trading Bot — Volatility-Adjusted Trend Following

An autonomous crypto spot-trading bot for the Roostoo mock exchange, built for the
HK vs AU vs IN Quant Trading Hackathon (Susquehanna × Roostoo). It makes all buy, hold
and sell decisions itself through the Roostoo REST API, with no manual intervention.

## Strategy

The competition ranks by portfolio return, then by a composite of Sortino (40%), Sharpe (30%)
and Calmar (30%). The design therefore aims for steady trend capture with tight control of
downside volatility and drawdown, rather than maximum raw return.

**Universe.** Every hour the bot takes the 10 most liquid USD pairs on Roostoo (by 24h traded
value), plus BTC/USD.

**Signals** (15-minute bars built from the live ticker):

| Component | Definition |
|---|---|
| Trend filter | price > 24h EMA **and** 6h EMA > 24h EMA |
| Momentum | 24h return > 0 |
| Score | 24h return ÷ (24h volatility × √96), a risk-adjusted momentum t-stat |
| Hysteresis | a coin already held only needs price > 24h EMA to stay, and gets a +0.25 score bonus. This cuts churn and fees. |

**Portfolio construction.**
- Hold up to 3 coins: the top-scoring eligible ones.
- Size by inverse volatility (calmer coins get more capital), capped at 35% per coin.
- **BTC regime filter:** if BTC is above its 24h EMA, up to 90% of equity is invested;
  otherwise at most 30%.
- Rebalances smaller than 2% of equity are skipped to save the 0.1% taker fee.

**Risk management.**
- **Trailing stop** per position: 1.5 × daily volatility below the post-entry peak,
  clamped to 4–12%. After a stop, the coin is not re-entered for 6 hours.
- **Drawdown circuit breaker:** if equity falls 10% from its peak, the bot goes to cash
  for 12 hours, then resumes with the peak reset.
- No leverage, no high-frequency trading: one ticker request per minute and one decision per hour.

**Daily activity safeguard.** The competition requires trades on at least 8 days. If the bot
has made no fill by 18:00 HKT on a given day (e.g. it sat in cash all day), it places one small
probe buy (2% of equity) in the highest-scoring coin. The normal hourly logic then manages or
exits that position. These trades are tagged `keepalive` in the trade log.

## Project structure

```
bot.py              entry point and main loop (poll every 60s, rebalance hourly)
config.py           all tunable parameters; secrets read from .env
roostoo_client.py   Roostoo REST client with HMAC-SHA256 request signing
paper_client.py     paper-trading wallet using live Roostoo prices (DRY_RUN=1)
data_store.py       builds and persists 15-minute price bars; optional Binance history bootstrap
strategy.py         pure signal and sizing functions (no I/O)
trader.py           turns targets into orders; stops, breaker, logging, state
tests/              offline tests (signature check vs. API docs, simulated market run)
```

Runtime files (not committed): `data/bars.csv`, `data/state.json`, `logs/bot.log`,
`logs/trades.csv` (every order attempt with success flag and error), `logs/equity.csv`
(equity, exposure and drawdown at every rebalance).

## Setup

Requires Python 3.9+.

```bash
git clone https://github.com/<your-username>/roostoo-trading-bot.git
cd roostoo-trading-bot
python3 -m pip install -r requirements.txt
cp .env.example .env      # then edit .env with your API keys
python3 -m tests.test_offline
```

### Paper test first

With `DRY_RUN=1` in `.env`, the bot uses real Roostoo prices but a simulated wallet:

```bash
python3 bot.py
```

### Go live on EC2

Set `DRY_RUN=0` in `.env`, then run inside tmux so the bot survives disconnects:

```bash
sudo dnf install -y tmux
tmux new -s bot
python3 bot.py
# detach: Ctrl+B then D   |   reattach: tmux attach -t bot
```

Monitor with `tail -f logs/bot.log` or `cat logs/trades.csv`.

## Price history

Signals need about 24h of history. On startup the bot fills missing history from Binance's public
15-minute klines (COINUSDT) so it can trade immediately; after that it relies only on its own bars
from the Roostoo ticker. If Binance is unreachable, the bot waits until it has collected enough
bars itself (logged as "Warming up").

## Changelog

- v1.0: initial trend-following strategy with regime filter, trailing stops and drawdown breaker.
