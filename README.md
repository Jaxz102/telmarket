# telmarket

Telegram bot that scrapes [MarketBeat insider trade alerts](https://www.marketbeat.com/instant-alerts/topics/insider-trade)
every 6 hours, keeps only **purchases** (buys / purchases / acquires), and posts them to a channel with a link to each article.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in TELEGRAM_TOKEN and TELEGRAM_CHAT_ID
```

### Getting the channel ID

1. In Telegram, open the channel → Administrators → Add admin → `@marksider_bot` (needs "Post messages").
2. Post any message in the channel.
3. Run `.venv/bin/python bot.py --discover` — it prints the channel's numeric ID (starts with `-100`).
4. Put that in `.env` as `TELEGRAM_CHAT_ID`.

## Running

```sh
.venv/bin/python bot.py --dry-run   # scrape and print, no posting
.venv/bin/python bot.py --once      # one check + post
.venv/bin/python bot.py             # loop forever, every INTERVAL_HOURS
```

### Every 6 hours via GitHub Actions (recommended, free)

`.github/workflows/insider-alerts.yml` runs `bot.py --once` on a 6-hour cron and commits `seen.json`
back to the repo so state survives between runs.

1. Push this repo to GitHub.
2. Repo → Settings → Secrets and variables → Actions → add `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`.
3. Actions tab → "Insider purchase alerts" → Run workflow, to test.

Note: GitHub disables scheduled workflows after 60 days with no commits — the state commits keep it alive.

### Running locally on a schedule

`python bot.py` (no flags) loops forever, checking every `INTERVAL_HOURS`. Don't run it alongside the
GitHub Action — each keeps its own `seen.json`, so you'd get duplicate posts.

## How it works

- Fetches listing pages (`/page/N/`) until it reaches articles older than `LOOKBACK_HOURS` (default `INTERVAL_HOURS + 1`, i.e. 7).
- A title counts as a purchase if it contains buy/purchase/acquire and **not** sell/sale/dispose.
- Posted URLs are recorded in `seen.json` so nothing is sent twice even if runs overlap.
- Messages are chunked to stay under Telegram's 4096-character limit.
