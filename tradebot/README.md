# Swing Trade Bot (Alpaca, paper trading)

Once a day, the bot reads four "smart money" feeds and checks each flagged stock against a trend filter. It then buys the best few, each with a stop-loss and a take-profit attached. Positions are sold when the trend breaks or after 30 days.

```
insider buys ─┐
congress ─────┤──> follow score ──┐
hedge fund 13F┤                   ├──> final score ──> risk checks ──> bracket buy
reddit buzz ──┘   trend filter ───┘                                     (stop + target)
```

## Setup (about 10 minutes)

1. Create a free account at alpaca.markets. Open **Paper Trading** and generate API keys.
2. Install Python 3.11 or newer, then run:
   ```
   pip install -r requirements.txt
   cp .env.example .env        # paste your Alpaca keys into .env
   ```
3. In `config.yaml`, set `sec_user_agent` to your name and email. The SEC requires this for its data.
4. Optional: add a `QUIVER_API_KEY` to turn on congressional trades. That's a paid Quiver Quant plan. Without the key, that source is skipped.

## Run

| Command | What it does |
|---|---|
| `python -m bot.main --dry-run` | Runs everything and prints what it *would* buy or sell. No orders are placed. |
| `python -m bot.main --once` | One real run, placing paper orders. |
| `python -m bot.main --loop` | Keeps running and trades at 10:00 ET each weekday. |
| `streamlit run dashboard.py` | Shows equity, orders, signals and warnings in your browser. |
| `python -m pytest -q` | Runs the offline tests (no keys needed). |

For `--loop`, leave the computer on. Alternatively, schedule `--once` with cron or Windows Task Scheduler for 10:00 ET on weekdays.

## Safety rails (all in `config.yaml`)

- **Paper by default.** Real money needs both `mode: live` in the config *and* `CONFIRM_LIVE=YES` in `.env`.
- **1% risk per trade.** The position is sized so that hitting the stop loses at most 1% of equity.
- **Exposure limits.** At most 10% of equity in one stock, 80% invested overall, 8 positions, and 2 new buys per day.
- **Daily stop.** No new buys on a day the account is down 3%.
- **Drawdown halt.** All buying stops if equity falls 15% from its peak. Review before resuming.
- **No same-day sells.** This avoids pattern-day-trader flags.
- **Failure handling.** If a data feed fails, it is skipped. A broken feed never triggers a trade.

## Where the data comes from

| Source | Feed | Lag | Key |
|---|---|---|---|
| Insider buys | OpenInsider (SEC Form 4) | ~2 days | none |
| Congress | Quiver Quant API | weeks | paid |
| Hedge funds | SEC EDGAR 13F + OpenFIGI | up to 45 days | none |
| Social | ApeWisdom (Reddit) | real time | none |
| Prices | Alpaca (IEX feed) | real time | Alpaca |

## Before you consider going live

- Run it on paper for **at least 2–3 months** and compare the results against simply holding SPY.
- The scoring weights and thresholds are starting guesses and haven't been backtested. Tune them using what the paper journal shows.
- Following filings means acting on old information. Social buzz in particular often peaks right before a drop.
- This is software, not financial advice. You are responsible for what it trades.
