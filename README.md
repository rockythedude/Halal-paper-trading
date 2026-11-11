# Halal Paper Trader

Paper-money bot for a Shariah-screened list of US stocks and a short allow-list of large-cap utility coins. It marks the book with real public prices. There is no brokerage, no real order, and no API key.

The halal screen is a **working assumption, not a fatwa**, and it is not investment advice.

## Dashboard

https://rockythedude.github.io/Halal-paper-trading/

GitHub Pages serves the static files in `web/`. The page loads `data.json` from the same directory (cache-busted) and polls again every 45 seconds. The tape uses only prices already stored on the paper book from the last tick. It does not invent a price or a percent change.

## How a tick runs

`.github/workflows/tick.yml` runs on `schedule` (`*/5 * * * *`) and on `workflow_dispatch`:

1. Check out `main` and install `requirements.txt` with the pip cache.
2. Run `tick.py` (one pass: scout, filter, analyst, executor, risk, logger).
3. Run `export_page.py`, which rewrites `web/data.json` from `state.json`.
4. Commit `state.json` and `web/data.json` back to `main` as `github-actions[bot]` only when they changed. The message includes `[skip ci]`.
5. Deploy `web/` with `actions/upload-pages-artifact` and `actions/deploy-pages` in this same workflow.

A concurrency group keeps two ticks from running at once. GitHub sometimes starts a `schedule` run later than the cron expression; **Run workflow** on the Actions tab starts one immediately.

Pages is published by this workflow (`build_type: workflow`), not by a `docs/` branch.

## The paper book

`config.json` holds the starting capital (`100`), the night target (`6000`), the crypto allow-list, and the rules. `state.json` is the live ledger (cash, open positions, equity, log). The bot keeps trading from that file.

Changing `starting_capital` does not reset a book that already exists. To start over, replace `state.json` on purpose and let the next tick run.

## Change the rules

Edit `config.json` and push to `main`. The next tick reads it. Nothing in this repo takes an API key; do not add one.

| Key | What it does |
| --- | --- |
| `starting_capital` | Paper cash used only when `state.json` is missing |
| `target` | Night-target bar (default `$6,000`) |
| `max_position_pct` | Size cap per buy (default `0.08`) |
| `max_open_positions` | Book cap (default `8`) |
| `stop_loss_pct` | Hard stop (default `0.06`, shown as −6%) |
| `trailing_activate_pct` | Arm the trail after this gain (default `0.12`) |
| `trailing_stop_pct` | Trail distance once armed (default `0.05`) |
| `analyst_min_score` | Analyst kill line (default `0.6`) |
| `min_stock_adv_usd` / `min_crypto_volume_usd` | Liquidity floors |
| `crypto_universe` | Coins Filter is allowed to pass |
| `crypto_meme` / `crypto_lending_yield` | Hard rejects |
| `fee_bps` / `slippage_bps` | Paper fill cost |

Stocks fill only while the US cash session is open. Crypto can be marked and traded at any hour. The bot never averages down, and it does not short, use leverage, or trade futures.

## Data sources

No keys.

- **Stocks:** Nasdaq’s public quote API (daily history and last sale). If a name is missing, Yahoo Finance spark, then a one-symbol Yahoo chart. Yahoo is rate-limited and is only the fallback.
- **Crypto:** CoinGecko for spot, 24h volume, and 24h change; Binance Vision for hourly klines; Coinbase public candles if Binance is blocked; then Yahoo; then the CoinGecko market chart.
- **Screen membership:** the SP Funds daily SPUS holdings CSV. If that download fails, the bot uses `cache/spus_holdings.csv` checked into this repo and says so in the state note. The S&P 500 name list comes from the public `datasets` CSV, with `cache/sp500_constituents.csv` if that fetch fails.
- If both the live SPUS file and the saved copy fail, Filter falls back to an AAOIFI-style sector screen of the S&P 500. Debt/market cap is applied only when a real ratio is present. A missing ratio is not treated as a pass or a fail.

## What “halal” means here

A stock passes the screen when it is in the current SPUS holdings (SP Funds S&P 500 Sharia Industry Exclusions ETF). Filter also drops impermissible sectors (banks, insurance, alcohol, tobacco, gambling, pork, adult content, weapons). Crypto passes only when it is on the curated utility list in `config.json` (BTC, ETH, SOL, ADA, XRP, LINK, DOT, AVAX). Meme coins and lending or yield tokens are killed. The bot never stakes, lends, or earns yield.

Scholars disagree about these names, including proof-of-stake coins. This repository does not resolve that. The screen is a working assumption for a paper book, not a fatwa.

## Run one tick locally

```bash
pip install -r requirements.txt
python3 tick.py
python3 export_page.py
python3 -m http.server 8765 -d web
```

Open `http://127.0.0.1:8765/`. `tick.py` takes an exclusive `tick.lock` so a second run skips instead of overlapping.
