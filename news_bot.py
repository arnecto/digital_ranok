#!/usr/bin/env python3
"""
News digest bot.

Fetches RSS news from IT/AI, Business/Startups and Crypto sources,
filters items published in the last 24 hours, asks Claude to pick the
most important ones and write short summaries, then posts the digest
to a Telegram channel.

Setup:
    pip install feedparser requests

Environment variables required:
    TELEGRAM_BOT_TOKEN   - token from @BotFather
    TELEGRAM_CHAT_ID     - your channel id (e.g. @my_channel or -100123456789)
    GEMINI_API_KEY       - optional, enables AI filtering/summaries via
                            Google Gemini (free tier). Get one at
                            https://aistudio.google.com/apikey
                            Without it, the script just posts the first
                            N raw items per category.

Run manually:
    python news_bot.py

Schedule it (cron example, runs every day at 09:00 and 21:00):
    0 9,21 * * * cd /path/to/script && /usr/bin/python3 news_bot.py >> bot.log 2>&1
"""

import os
import time
import json
import feedparser
import requests
from datetime import datetime, timedelta, timezone

# ---------- CONFIG ----------

FEEDS = {
    "IT / AI": [
        "https://habr.com/ru/rss/all/all/",
        "https://hnrss.org/frontpage",
        "https://www.theverge.com/rss/index.xml",
    ],
    "Бізнес / Стартапи": [
        "https://techcrunch.com/feed/",
        "https://venturebeat.com/feed/",
    ],
    "Крипта": [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
    ],
}

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]  # bot: @digital_ranok_bot (token from BotFather, not the username)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "@digital_ranok")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")  # optional

CHANNEL_NAME = "Цифровий Ранок"
MAX_ITEMS_PER_CATEGORY = 5
HOURS_WINDOW = 24
GEMINI_MODEL = "gemini-flash-lite-latest"  # alias, always points to current fast/cheap free-tier model


def fetch_recent_entries():
    """Pull all RSS entries published within the last HOURS_WINDOW hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_WINDOW)
    results = {}
    for category, urls in FEEDS.items():
        entries = []
        for url in urls:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                published = entry.get("published_parsed") or entry.get("updated_parsed")
                if not published:
                    continue
                pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                if pub_dt >= cutoff:
                    entries.append({
                        "title": entry.get("title", "").strip(),
                        "link": entry.get("link", ""),
                        "summary": (entry.get("summary", "") or "")[:400],
                        "published": pub_dt.isoformat(),
                        "source": feed.feed.get("title", url),
                    })
        results[category] = entries
    return results


def rank_and_summarize(category, entries):
    """Ask Gemini to pick the most interesting items and summarize them in Ukrainian."""
    if not entries:
        return []
    if not GEMINI_API_KEY:
        return entries[:MAX_ITEMS_PER_CATEGORY]

    items_text = "\n\n".join(
        f"[{i}] {e['title']}\nДжерело: {e['source']}\n{e['summary']}"
        for i, e in enumerate(entries)
    )

    prompt = (
        f'Ось список новин за категорією "{category}" за останні {HOURS_WINDOW} години.\n'
        f"Обери не більше {MAX_ITEMS_PER_CATEGORY} найважливіших і найцікавіших новин.\n"
        "Для кожної напиши коротке summary (2-3 речення) українською.\n"
        'Поверни ЛИШЕ JSON-масив об\'єктів формату:\n'
        '[{"index": <номер з дужок>, "summary": "<текст>"}]\n'
        "Без жодного іншого тексту, без markdown-обгортки.\n\n"
        f"Новини:\n{items_text}"
    )

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    try:
        resp = requests.post(
            url,
            headers={
                "x-goog-api-key": GEMINI_API_KEY,
                "Content-Type": "application/json",
            },
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (requests.exceptions.RequestException, KeyError, IndexError) as e:
        print(f"[WARN] Gemini call failed for '{category}': {e}. Falling back to raw items.")
        return entries[:MAX_ITEMS_PER_CATEGORY]

    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        picks = json.loads(text)
    except json.JSONDecodeError:
        return entries[:MAX_ITEMS_PER_CATEGORY]

    selected = []
    for p in picks:
        idx = p.get("index")
        if idx is None or idx >= len(entries):
            continue
        item = entries[idx].copy()
        item["ai_summary"] = p.get("summary", item["summary"])
        selected.append(item)
    return selected


def format_message(category, items):
    if not items:
        return None
    lines = [f"📰 <b>{category}</b>\n"]
    for item in items:
        summary = item.get("ai_summary", item["summary"])
        lines.append(f"• <b>{item['title']}</b>\n{summary}\n{item['link']}\n")
    return "\n".join(lines)


def send_to_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    })
    resp.raise_for_status()


def main():
    data = fetch_recent_entries()

    today = datetime.now().strftime("%d.%m.%Y")
    send_to_telegram(f"🌅 <b>{CHANNEL_NAME}</b> — дайджест за {today}")
    time.sleep(1)

    for category, entries in data.items():
        selected = rank_and_summarize(category, entries)
        message = format_message(category, selected)
        if message:
            # Telegram message limit is ~4096 chars, split if needed
            for chunk_start in range(0, len(message), 4000):
                send_to_telegram(message[chunk_start:chunk_start + 4000])
                time.sleep(1)
            print(f"[OK] Posted {len(selected)} items for '{category}'")
        else:
            print(f"[SKIP] No news for '{category}' in last {HOURS_WINDOW}h")


if __name__ == "__main__":
    main()
