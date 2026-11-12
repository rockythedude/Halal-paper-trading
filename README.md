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

The first deploy needs Pages switched on by hand: **Settings → Pages → Build and deployment → Source: GitHub Actions**. Then run **Actions → Paper tick → Run workflow** once. After that, the schedule keeps going. The paper book is committed even if that deploy step cannot run yet.

## The paper book

`config.json` holds the starting capital (`100`), the night target (`6000`), the crypto allow-list, and the rules. `state.json` is the live ledger (cash, open positions, equity, log). The bot keeps trading from that file.

Changing `starting_capital` does not reset a book that already exists. To start over, replace `state.json` on purpose and let the next tick run.

## Change the rules

Edit `config.json` and push to `main`. The next tick reads it. Nothing in this repo takes an API key; do not add one.

| Key | What it does |
| --- | --- |
| `starting_capital` | Paper cash used only when `state.json` is missing |
| `target` | Night-target bar (default `$6,000`) |
| `max_position_pct` | Size cap per buy (default `0.08`). Buys that cost more than cash are skipped. No margin. |
| `max_open_positions` | Book cap (default `12`) |
| `max_stock_positions` | Stock sleeve (default `8`). A full sleeve blocks new stock buys only. |
| `max_crypto_positions` | Crypto sleeve (default `4`). Crypto can fill at any hour. |
| `mode` | `fast` is the default risk pack |
| `stop_loss_pct` | Hard stop (default `0.05`, shown as −5% on the main book and the meme desk). Applied to open positions on the next tick. The copy desk keeps −2%. |
| `trailing_activate_pct` | Arm the trail after this gain (default `0.03`) |
| `trailing_stop_pct` | Once armed, sell this far off the peak (default `0.01`) |
| `take_profit_pct` | Sell outright at this gain (default `0.05`) |
| `analyst_min_score` | Analyst kill line on the main book (default `0.55`) |
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

## Three paper desks

All three use fast-mode risk (stop −2%, trail arms at +3% and sells 1% off the peak, take profit +5%), size at most 8% of equity, and never spend cash they do not have.

| Desk | URL | Book |
| --- | --- | --- |
| Main halal book | https://rockythedude.github.io/Halal-paper-trading/ | `state.json` · 12 slots, 8 stocks and 4 crypto |
| Copy | https://rockythedude.github.io/Halal-paper-trading/copy/ | `copy/state.json` · 10 slots |
| Crypto + Meme | https://rockythedude.github.io/Halal-paper-trading/meme/ | `meme/state.json` · 10 slots |
| BTC Scalper | https://rockythedude.github.io/Halal-paper-trading/btc/ | `btc/state.json` · BTC spot, up to 4 lots |
| Wallet copy | https://rockythedude.github.io/Halal-paper-trading/wallets/ | `wallets/state.json` · Solana meme wallets, 10% slots, max 8 |
| Axiom scalper | https://rockythedude.github.io/Halal-paper-trading/scalp/ | `scalp/state.json` · hot Solana memes, 10% slots, max 6 |

The main page leaderboard reads all three books. Each page links to the others.

### Copy desk

The old House and Senate Stock Watcher S3 feeds return Access Denied. The copy desk reads the public Capitol Stock Watch CSV (House and Senate STOCK Act disclosures, no key). If that download fails, it uses the OptimistFi House JSON and says so. A purchase is eligible only when the filing date is within the last 45 days, the row is a stock purchase with a real ticker, and that ticker is on the current SPUS list. The log line names the member and both dates, for example `copied $NVDA - Rep. X bought 2026-10-20, disclosed 2026-11-05`.

Those filings are already late. The page says the copy happens days or weeks after the original trade.

A short 13F watch list (Berkshire, Pershing Square, Duquesne, Third Point) is checked on SEC EDGAR. A report inside the 45-day window is logged and not traded: the form publishes CUSIPs, and this bot will not guess a ticker. There is no free keyless feed of a known crypto wallet's recent buys, so the copy desk does not copy crypto.

### Crypto + Meme

This page is **not** halal-screened. It is a speculative sandbox. The universe is CoinGecko's public meme-token list (DOGE, SHIB, PEPE, BONK, WIF, FLOKI and the other liquid names on that list). Stablecoins and leveraged tokens are dropped. A buy requires a positive 24h change of at most 80%, at least $10 million of reported 24h volume, and a market cap of at least $50 million when CoinGecko publishes one. Larger 24h prints are ignored rather than traded. If CoinGecko is down, Binance public 24h tickers are used for the named meme list only. The desk is labelled **momentum**, not copy, because no keyless wallet-buy feed was available.

### BTC Scalper

https://rockythedude.github.io/Halal-paper-trading/btc/ trades only spot BTC, with no leverage, short, or margin. Each entry is 20% of equity, and at most four entries are open (80% invested). A 0.1% taker fee is charged on each side. Prices are Binance public BTCUSDT 1-minute and 5-minute candles, with CoinGecko as the fallback. It buys when a 5-minute EMA9 crosses above EMA21, when 5-minute RSI(14) is below 35 and turning up, or when price breaks the prior 30-minute high on rising volume. Each lot takes profit at +0.8%, stops at −0.6%, or trails 0.4% once it is up 0.5%. Those are the only exits. After a hard stop the next evaluation does not enter. Inside each workflow run the scalper checks about once a minute for roughly four minutes, while the other desks tick, so the job stays near the five-minute schedule. The page says this is BTC spot only, inside the crypto allow-list assumption, and not a fatwa.

### Wallet copy (Solana memes)

https://rockythedude.github.io/Halal-paper-trading/wallets/ is **not** halal-screened. It paper-copies a pinned list of 10 wallets from public leaderboards: four from GMGN's Smart Degen tab and six named wallets from the Kolscan leaderboard. The list is not replaced by the auto scan. Each row stores that source and the leaderboard stat, and the page adds this book's own copied P&L for that wallet. Copies from the earlier auto list are left open until that wallet sells or the −25% / +100% backstop hits, and they do not open new positions.

A DEX copy is 10% of equity when DexScreener liquidity is at least $20,000, with a 1% fee each side. A Pump.fun token that is still on its bonding curve, and has no such pool, is 5% of equity with a 2% fee. Its price is the curve account's virtual SOL and token reserves, read from Solana's public RPC, times a public SOL/USD price. If that account is missing, already migrated, or the SOL price is missing, the buy is skipped rather than priced from nothing. After the token migrates, later marks and exits use the DexScreener price. At most 8 positions are open. The wallet's own sell closes the copy. A −25% stop and a +100% take profit are backstops. The scoreboard shows each wallet's copied realized P&L, wins, and losses. A wallet with at least 5 closed copies and negative realized P&L is paused: no new copies, and any open copy can still exit. Curve positions and their log lines are tagged `pump.fun curve`. `copy-live.yml` keeps a websocket open for about 5 hours 50 minutes and starts the next run before GitHub's 6-hour limit. With `HELIUS_API_KEY` set it copies a swap within seconds of the chain event and stores the wallet's trade time and our fill time. Without the key it keeps the 12-second poll and the page says `waiting for Helius key`. The page reads `raw.githubusercontent.com` every 2 seconds and keeps that file when its timestamp is newer than the Pages copy. A change is committed as soon as the previous push has finished, about every 2 seconds, and a new fill is pushed immediately. The 5-minute tick no longer polls these wallets. `web/wallets/replay.json` is a 30-day paper replay of the same 10 wallets, regenerated when it is older than about a day. Each copied buy is $10, fees match the live bot, and the fill is the next real on-chain price at least 15 seconds later. Buys that cannot be priced are counted and skipped.

### Axiom scalper

https://rockythedude.github.io/Halal-paper-trading/scalp/ is **not** halal-screened. It paper-trades hot Solana pairs from DexScreener's boost and profile lists and from GeckoTerminal's trending and new Solana pools. A pair has to show at least $25k liquidity, a market cap from $100k to $100M, at least $5k of 5-minute volume, 10 minutes of age, a buy/sell ratio of at least 1.05, and a 5-minute move between +1% and +40%. Volume that is hotter than the hourly pace ranks a name higher; it does not block one. Each buy is 10% of equity, at most 6 are open, and each side pays a 1% fee plus 0.5% slippage. Exits are +8%, −5%, a trail 2.5% off the peak after +4%, or a close at 20 minutes if the trail never armed. The page polls about every 8 seconds. A coin links to `axiom.trade/meme/<token>?chain=sol`.
