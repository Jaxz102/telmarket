#!/usr/bin/env python3
"""Telegram bot that posts MarketBeat insider *purchases* to a channel every 6 hours.

Usage:
    python bot.py               # run forever, checking every INTERVAL_HOURS
    python bot.py --once        # single check + post (for cron/launchd)
    python bot.py --dry-run     # scrape and print, don't post
    python bot.py --discover    # print chat IDs the bot can see (to find the channel ID)
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

def _env(name: str, default: str = "") -> str:
    """Read an env var, tolerating a pasted 'NAME=value' line, quotes, and stray whitespace."""
    value = os.getenv(name, default).strip().strip("'\"")
    prefix = f"{name}="
    if value.startswith(prefix):
        value = value[len(prefix):].strip().strip("'\"")
    return value


TELEGRAM_TOKEN = _env("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID")
INTERVAL_HOURS = float(os.getenv("INTERVAL_HOURS", "6"))
# Look back a bit further than the interval so a late run (e.g. Mac was asleep) doesn't miss anything;
# seen.json prevents re-posting the overlap.
LOOKBACK_HOURS = float(os.getenv("LOOKBACK_HOURS", str(INTERVAL_HOURS + 1)))
MAX_PAGES = int(os.getenv("MAX_PAGES", "10"))
STATE_FILE = BASE_DIR / os.getenv("STATE_FILE", "seen.json")

EASTERN = ZoneInfo("America/New_York")
LIST_URL = "https://www.marketbeat.com/instant-alerts/topics/insider-trade/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

BUY_RE = re.compile(r"\b(buy|buys|buying|bought|purchase|purchases|purchased|acquire|acquires|acquired)\b", re.I)
SELL_RE = re.compile(r"\b(sell|sells|selling|sold|sale|dispose|disposes|disposed)\b", re.I)

log = logging.getLogger("telmarket")


# --------------------------------------------------------------------------- scraping

def fetch_page(page: int) -> str:
    url = LIST_URL if page == 1 else f"{LIST_URL}page/{page}/"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_published(card) -> datetime | None:
    """Recent cards carry <time datetime="...Z">; older ones a plain 'September 1, 2026 3:08 PM ET'."""
    time_el = card.select_one("time[datetime]")
    if time_el is not None:
        dt = datetime.fromisoformat(time_el["datetime"].replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    post_time = card.select_one(".post-time")
    if post_time is None:
        return None
    text = post_time.get_text(" ", strip=True).replace(" ET", "")
    try:
        return datetime.strptime(text, "%B %d, %Y %I:%M %p").replace(tzinfo=EASTERN)
    except ValueError:
        log.warning("unparsed post time: %r", text)
        return None


def parse_cards(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for card in soup.select("div.content-card"):
        title_el = card.select_one("h3.content-card-title")
        link_el = card.select_one("a.stretched-link[href]")
        published = parse_published(card)
        if not (title_el and link_el and published):
            continue
        sym = card.select_one("[data-symbol]")
        ticker = None
        if sym is not None:
            ticker = f"{sym.get('data-prefix', '')}:{sym.get('data-symbol', '')}".strip(":")
        items.append(
            {
                "title": title_el.get_text(strip=True),
                "url": link_el["href"],
                "published": published,
                "ticker": ticker,
            }
        )
    return items


def is_purchase(title: str) -> bool:
    return bool(BUY_RE.search(title)) and not SELL_RE.search(title)


def scrape_purchases(lookback_hours: float) -> list[dict]:
    """Return insider-purchase articles published within the lookback window, newest first."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    results: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        cards = parse_cards(fetch_page(page))
        if not cards:
            break
        for c in cards:
            if c["published"] >= cutoff and is_purchase(c["title"]):
                results.append(c)
        oldest = min(c["published"] for c in cards)
        log.debug("page %d: %d cards, oldest %s", page, len(cards), oldest.isoformat())
        if oldest < cutoff:
            break
    # de-dupe by url, keep order
    seen, unique = set(), []
    for r in results:
        if r["url"] not in seen:
            seen.add(r["url"])
            unique.append(r)
    return unique


# --------------------------------------------------------------------------- state

def load_seen() -> dict[str, str]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("state file corrupt, starting fresh")
    return {}


def save_seen(seen: dict[str, str]) -> None:
    # prune entries older than 7 days so the file doesn't grow forever
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    pruned = {u: ts for u, ts in seen.items() if datetime.fromisoformat(ts) >= cutoff}
    STATE_FILE.write_text(json.dumps(pruned, indent=1))


# --------------------------------------------------------------------------- telegram

def tg(method: str, **params):
    resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}", json=params, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {data}")
    return data["result"]


def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_messages(items: list[dict]) -> list[str]:
    header = f"🟢 <b>Insider Purchases</b> — {len(items)} new\n\n"
    lines = []
    for it in items:
        ticker = f"<b>{html_escape(it['ticker'])}</b> · " if it["ticker"] else ""
        lines.append(f"• {ticker}<a href=\"{it['url']}\">{html_escape(it['title'])}</a>")
    # Telegram caps messages at 4096 chars; chunk if needed.
    messages, current = [], header
    for line in lines:
        if len(current) + len(line) + 1 > 4000:
            messages.append(current.rstrip())
            current = ""
        current += line + "\n"
    messages.append(current.rstrip())
    return messages


def send(items: list[dict]) -> None:
    for text in format_messages(items):
        tg("sendMessage", chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="HTML",
           disable_web_page_preview=True)
        time.sleep(1)


# --------------------------------------------------------------------------- main

def run_once(dry_run: bool = False) -> None:
    seen = load_seen()
    items = scrape_purchases(LOOKBACK_HOURS)
    new = [it for it in items if it["url"] not in seen]
    log.info("found %d purchases in window, %d new", len(items), len(new))

    if dry_run:
        for it in new:
            print(f"{it['published'].isoformat()}  {it['ticker'] or '-':14}  {it['title']}\n    {it['url']}")
        return

    if not new:
        if not items:
            log.info("no insider purchases found in the last %g h — no Telegram message sent", LOOKBACK_HOURS)
        else:
            log.info("%d purchase(s) in window but all already posted — no Telegram message sent", len(items))
        return

    send(new)
    for it in new:
        seen[it["url"]] = it["published"].isoformat()
    save_seen(seen)
    log.info("posted %d items", len(new))


def discover() -> None:
    """Print every chat the bot has seen an update from (channel posts, group messages, DMs)."""
    updates = tg("getUpdates", allowed_updates=["channel_post", "message", "my_chat_member"])
    chats = {}
    for u in updates:
        for key in ("channel_post", "message", "my_chat_member"):
            if key in u:
                chat = u[key]["chat"]
                chats[chat["id"]] = f"{chat.get('type')}: {chat.get('title') or chat.get('username') or ''}"
    if not chats:
        print("No updates yet. Add the bot as an admin to the channel, post any message there, then re-run.")
        return
    for cid, desc in chats.items():
        print(f"{cid}\t{desc}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="run one check and exit")
    ap.add_argument("--dry-run", action="store_true", help="scrape and print, do not post")
    ap.add_argument("--discover", action="store_true", help="print chat IDs visible to the bot")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not TELEGRAM_TOKEN:
        sys.exit("TELEGRAM_TOKEN is not set in .env")
    try:
        me = tg("getMe")
    except Exception as e:
        sys.exit(f"TELEGRAM_TOKEN is invalid (Telegram rejected it): {e}\n"
                 "Check the value is just the token, e.g. 123456:ABC..., with no 'TELEGRAM_TOKEN=' prefix.")
    log.info("authenticated as @%s", me.get("username"))

    if args.discover:
        discover()
        return
    if not args.dry_run and not TELEGRAM_CHAT_ID:
        sys.exit("TELEGRAM_CHAT_ID is not set — run `python bot.py --discover` to find it")
    if args.once or args.dry_run:
        run_once(dry_run=args.dry_run)
        return

    log.info("starting loop: every %g h, lookback %g h", INTERVAL_HOURS, LOOKBACK_HOURS)
    while True:
        try:
            run_once()
        except Exception:
            log.exception("check failed")
        time.sleep(INTERVAL_HOURS * 3600)


if __name__ == "__main__":
    main()
