#!/usr/bin/env python3
"""
News digest bot with on-demand admin commands.

Fetches RSS news from IT/AI, Business/Startups and Crypto sources,
filters items published recently, skips items already posted before
(tracked in posted_links.json), asks Gemini to pick the most important
ones and write short summaries, then posts the digest to a Telegram
channel — but only when there's something actually new.

It also polls for admin commands (sent by you, in a private chat with
the bot) once per run and replies. Because this script only runs on a
schedule (no server running 24/7), command replies arrive on the NEXT
scheduled run, not instantly — usually within an hour.

Setup:
    pip install feedparser requests

Environment variables required:
    TELEGRAM_BOT_TOKEN     - token from @BotFather
    TELEGRAM_CHAT_ID       - channel id (e.g. @my_channel or -100123456789)
    TELEGRAM_ADMIN_CHAT_ID - your personal chat id; required for commands
    GEMINI_API_KEY          - optional, enables AI filtering/summaries via
                               Google Gemini (free tier).
                               Without it, the script just posts the first
                               N raw items per category.

Run manually:
    python news_bot.py

Schedule it (cron example, runs every hour):
    0 * * * * cd /path/to/script && /usr/bin/python3 news_bot.py >> bot.log 2>&1

Admin commands (send these to the bot in a private chat):
    /stats or /today  - short report: how many posts today, and which
    /week             - same, for the last 7 days
    /sources          - list of active RSS sources
    /pause            - stop publishing to the channel (bot keeps running,
                        just won't post)
    /resume           - resume publishing
    /help             - list of commands
"""

import os
import time
import json
import feedparser
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID")  # your personal chat id
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")  # optional

CHANNEL_NAME = "Цифровий Ранок"
MAX_ITEMS_PER_CATEGORY = 5
HOURS_WINDOW = 3  # small buffer for hourly runs; dedup via posted_links.json prevents repeats anyway
GEMINI_MODEL = "gemini-flash-lite-latest"  # alias, always points to current fast/cheap free-tier model

POSTED_FILE = "posted_links.json"
POSTED_RETENTION_HOURS = 24 * 8  # keep 8 days of history so /week has data to show

BOT_STATE_FILE = "bot_state.json"

KYIV_TZ = ZoneInfo("Europe/Kyiv")

HELP_TEXT = (
    "🤖 <b>Команди:</b>\n"
    "/stats або /today — короткий звіт за сьогодні\n"
    "/week — звіт за останні 7 днів\n"
    "/sources — список RSS-джерел\n"
    "/pause — призупинити публікацію в канал\n"
    "/resume — відновити публікацію\n"
    "/help — цей список\n\n"
    "⏱ Бот перевіряє команди раз на годину (по розкладу), тож відповідь приходить не миттєво."
)


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


def send_to_telegram(text, chat_id=None):
    chat_id = chat_id or TELEGRAM_CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    })
    resp.raise_for_status()


def get_updates(offset):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    resp = requests.get(url, params={"offset": offset, "timeout": 0}, timeout=15)
    resp.raise_for_status()
    return resp.json().get("result", [])


# ---------- persistent state ----------

def load_posted():
    """Load {link: {title, category, posted_at}} of previously posted articles, pruning old entries."""
    if not os.path.exists(POSTED_FILE):
        return {}
    try:
        with open(POSTED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=POSTED_RETENTION_HOURS)
    pruned = {}
    for link, info in data.items():
        try:
            ts = info["posted_at"] if isinstance(info, dict) else info
            if datetime.fromisoformat(ts) >= cutoff:
                pruned[link] = info if isinstance(info, dict) else {"posted_at": info, "title": "", "category": ""}
        except (ValueError, KeyError):
            continue
    return pruned


def save_posted(posted):
    with open(POSTED_FILE, "w", encoding="utf-8") as f:
        json.dump(posted, f, ensure_ascii=False, indent=2)


def load_bot_state():
    if not os.path.exists(BOT_STATE_FILE):
        return {"last_update_id": 0, "paused": False}
    try:
        with open(BOT_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"last_update_id": 0, "paused": False}


def save_bot_state(state):
    with open(BOT_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------- admin commands ----------

def build_stats_message(posted, scope="today"):
    now = datetime.now(KYIV_TZ)
    items = list(posted.values())

    if scope == "today":
        items = [v for v in items if datetime.fromisoformat(v["posted_at"]).astimezone(KYIV_TZ).date() == now.date()]
        title = f"📊 <b>Звіт за сьогодні</b> ({now.strftime('%d.%m.%Y')})"
    else:
        cutoff = now - timedelta(days=7)
        items = [v for v in items if datetime.fromisoformat(v["posted_at"]) >= cutoff]
        title = "📊 <b>Звіт за останні 7 днів</b>"

    if not items:
        return f"{title}\n\nПоки що нічого не публікувалось."

    by_cat = {}
    for it in items:
        by_cat.setdefault(it.get("category", "Інше"), []).append(it["title"])

    lines = [title, f"Всього постів: {len(items)}\n"]
    for cat, titles in by_cat.items():
        lines.append(f"<b>{cat}</b> — {len(titles)}:")
        for t in titles:
            lines.append(f"• {t}")
        lines.append("")
    return "\n".join(lines)


def build_sources_message():
    lines = ["📡 <b>Активні джерела:</b>\n"]
    for cat, urls in FEEDS.items():
        lines.append(f"<b>{cat}</b>:")
        for u in urls:
            lines.append(f"  {u}")
        lines.append("")
    return "\n".join(lines)


def handle_admin_commands(posted, state):
    if not TELEGRAM_ADMIN_CHAT_ID:
        return state

    try:
        updates = get_updates(state.get("last_update_id", 0) + 1)
    except requests.exceptions.RequestException as e:
        print(f"[WARN] getUpdates failed: {e}")
        return state

    max_id = state.get("last_update_id", 0)
    for upd in updates:
        max_id = max(max_id, upd.get("update_id", max_id))
        msg = upd.get("message")
        if not msg:
            continue
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if chat_id != str(TELEGRAM_ADMIN_CHAT_ID) or not text.startswith("/"):
            continue

        command = text.split()[0].lower()
        print(f"[CMD] Received {command} from admin")

        if command in ("/stats", "/today", "/report"):
            send_to_telegram(build_stats_message(posted, scope="today"), chat_id=TELEGRAM_ADMIN_CHAT_ID)
        elif command == "/week":
            send_to_telegram(build_stats_message(posted, scope="week"), chat_id=TELEGRAM_ADMIN_CHAT_ID)
        elif command == "/sources":
            send_to_telegram(build_sources_message(), chat_id=TELEGRAM_ADMIN_CHAT_ID)
        elif command == "/pause":
            state["paused"] = True
            send_to_telegram("⏸ Публікацію в канал призупинено. /resume — щоб увімкнути назад.", chat_id=TELEGRAM_ADMIN_CHAT_ID)
        elif command == "/resume":
            state["paused"] = False
            send_to_telegram("▶️ Публікацію відновлено.", chat_id=TELEGRAM_ADMIN_CHAT_ID)
        elif command == "/help" or command == "/start":
            send_to_telegram(HELP_TEXT, chat_id=TELEGRAM_ADMIN_CHAT_ID)
        else:
            send_to_telegram("Не знаю такої команди.\n\n" + HELP_TEXT, chat_id=TELEGRAM_ADMIN_CHAT_ID)

    state["last_update_id"] = max_id
    return state


def main():
    state = load_bot_state()
    posted = load_posted()

    state = handle_admin_commands(posted, state)
    save_bot_state(state)

    if state.get("paused"):
        print("[INFO] Publishing is paused (via /pause). Skipping fetch/publish this run.")
        return

    data = fetch_recent_entries()

    # Drop items that were already posted in a previous run
    for category, entries in data.items():
        data[category] = [e for e in entries if e["link"] not in posted]

    # First pass: figure out what's actually new before sending anything
    to_publish = {}
    for category, entries in data.items():
        selected = rank_and_summarize(category, entries)
        if selected:
            to_publish[category] = selected
        else:
            print(f"[SKIP] No new news for '{category}' in last {HOURS_WINDOW}h")

    if not to_publish:
        print("[INFO] Nothing new this run, channel not touched.")
        return

    today = datetime.now(KYIV_TZ).strftime("%d.%m.%Y")
    now_time = datetime.now(KYIV_TZ).strftime("%H:%M")

    send_to_telegram(f"🌅 <b>{CHANNEL_NAME}</b> — новини за {today} {now_time}")
    time.sleep(1)

    for category, selected in to_publish.items():
        message = format_message(category, selected)
        # Telegram message limit is ~4096 chars, split if needed
        for chunk_start in range(0, len(message), 4000):
            send_to_telegram(message[chunk_start:chunk_start + 4000])
            time.sleep(1)
        print(f"[OK] Posted {len(selected)} items for '{category}'")

        for item in selected:
            posted[item["link"]] = {
                "title": item["title"],
                "category": category,
                "posted_at": datetime.now(KYIV_TZ).isoformat(),
            }

    save_posted(posted)


if __name__ == "__main__":
    main()
