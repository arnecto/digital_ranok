#!/usr/bin/env python3
"""
News digest bot with a throttled queue and on-demand admin commands.

Fetches RSS news from IT/AI, Business/Startups and Crypto sources,
filters items published recently, and queues new ones (deduplicated by
link) into queue.json. Every run checks whether an hour has passed
since the last publish — if so, it pops ONE item from the queue
(round-robin across categories) and posts it to the Telegram channel.
This guarantees roughly 1 post per hour, no bursts, even if many new
articles appear at once.

It also polls for admin commands (sent by you, in a private chat with
the bot) on every run and replies. With the schedule set to every
5 minutes, replies arrive within a few minutes, not instantly.

Setup:
    pip install feedparser requests

Environment variables required:
    TELEGRAM_BOT_TOKEN     - token from @BotFather
    TELEGRAM_CHAT_ID       - channel id (e.g. @my_channel or -100123456789)
    TELEGRAM_ADMIN_CHAT_ID - your personal chat id; required for commands
    GEMINI_API_KEY          - optional, enables AI filtering/summaries via
                               Google Gemini (free tier).
                               Without it, the script just queues the first
                               N raw items per category, no summaries.

Run manually:
    python news_bot.py

Schedule it (cron example, runs every 5 minutes):
    */5 * * * * cd /path/to/script && /usr/bin/python3 news_bot.py >> bot.log 2>&1

Admin commands (send these to the bot in a private chat):
    /stats or /today  - short report: how many posts today, and which
    /week             - same, for the last 7 days
    /queue            - how many articles are waiting in the queue
    /sources          - list of active RSS sources
    /pause            - stop publishing to the channel (bot keeps queuing,
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
CATEGORY_ORDER = list(FEEDS.keys())  # fixed order for round-robin

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]  # bot: @digital_ranok_bot (token from BotFather, not the username)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "@digital_ranok")
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID")  # your personal chat id
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")  # optional

CHANNEL_NAME = "Цифровий Ранок"
MAX_NEW_PER_CATEGORY_PER_RUN = 5  # cap on how many new items get queued from a single fetch
HOURS_WINDOW = 3  # small buffer so nothing is missed between runs; queue.json prevents repeats
GEMINI_MODEL = "gemini-flash-lite-latest"  # alias, always points to current fast/cheap free-tier model

PUBLISH_INTERVAL = timedelta(hours=1)  # exactly one post per hour
QUEUE_MAX_AGE_HOURS = 48  # drop queued items older than this — stale news isn't worth posting late

POSTED_FILE = "posted_links.json"
POSTED_RETENTION_HOURS = 24 * 8  # keep 8 days of history so /week has data to show

QUEUE_FILE = "queue.json"
BOT_STATE_FILE = "bot_state.json"

KYIV_TZ = ZoneInfo("Europe/Kyiv")

HELP_TEXT = (
    "🤖 <b>Команди:</b>\n"
    "/stats або /today — короткий звіт за сьогодні\n"
    "/week — звіт за останні 7 днів\n"
    "/queue — скільки новин чекає в черзі\n"
    "/sources — список RSS-джерел\n"
    "/pause — призупинити публікацію в канал\n"
    "/resume — відновити публікацію\n"
    "/help — цей список\n\n"
    "⏱ Публікується рівно 1 новина на годину. Команди перевіряються кожні кілька хвилин."
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


def rank_and_summarize(category, entries, max_items):
    """Ask Gemini to pick the most interesting items and summarize them in Ukrainian."""
    if not entries:
        return []
    if not GEMINI_API_KEY:
        return entries[:max_items]

    items_text = "\n\n".join(
        f"[{i}] {e['title']}\nДжерело: {e['source']}\n{e['summary']}"
        for i, e in enumerate(entries)
    )

    prompt = (
        f'Ось список новин за категорією "{category}".\n'
        f"Обери не більше {max_items} найважливіших і найцікавіших новин.\n"
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
        return entries[:max_items]

    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        picks = json.loads(text)
    except json.JSONDecodeError:
        return entries[:max_items]

    selected = []
    for p in picks:
        idx = p.get("index")
        if idx is None or idx >= len(entries):
            continue
        item = entries[idx].copy()
        item["ai_summary"] = p.get("summary", item["summary"])
        selected.append(item)
    return selected


def summarize_single(category, item):
    """Get (or reuse) a nice Ukrainian summary for exactly one item, right before publishing."""
    if item.get("ai_summary"):
        return item
    result = rank_and_summarize(category, [item], max_items=1)
    return result[0] if result else item


def format_single_message(category, item):
    summary = item.get("ai_summary", item["summary"])
    return f"📰 <b>{category}</b>\n\n<b>{item['title']}</b>\n{summary}\n{item['link']}"


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

def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_posted():
    """Load {link: {title, category, posted_at}} of previously posted articles, pruning old entries."""
    data = load_json(POSTED_FILE, {})
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
    save_json(POSTED_FILE, posted)


def load_queue():
    """{category: [items]}, pruning items older than QUEUE_MAX_AGE_HOURS."""
    data = load_json(QUEUE_FILE, {})
    cutoff = datetime.now(timezone.utc) - timedelta(hours=QUEUE_MAX_AGE_HOURS)
    cleaned = {}
    for category in CATEGORY_ORDER:
        items = data.get(category, [])
        fresh = []
        for it in items:
            try:
                if datetime.fromisoformat(it["published"]) >= cutoff:
                    fresh.append(it)
            except (ValueError, KeyError):
                continue
        cleaned[category] = fresh
    return cleaned


def save_queue(queue):
    save_json(QUEUE_FILE, queue)


def load_bot_state():
    return load_json(BOT_STATE_FILE, {"last_update_id": 0, "paused": False, "last_publish_at": None, "rr_index": 0})


def save_bot_state(state):
    save_json(BOT_STATE_FILE, state)


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


def build_queue_message(queue):
    total = sum(len(v) for v in queue.values())
    if total == 0:
        return "📭 Черга порожня — все актуальне вже опубліковано."
    lines = [f"📥 <b>В черзі: {total}</b>\n"]
    for cat in CATEGORY_ORDER:
        n = len(queue.get(cat, []))
        if n:
            lines.append(f"<b>{cat}</b> — {n}")
    hours_to_drain = total  # ~1 per hour
    lines.append(f"\nПри темпі 1/годину — черга спорожніє приблизно за {hours_to_drain} год.")
    return "\n".join(lines)


def build_sources_message():
    lines = ["📡 <b>Активні джерела:</b>\n"]
    for cat, urls in FEEDS.items():
        lines.append(f"<b>{cat}</b>:")
        for u in urls:
            lines.append(f"  {u}")
        lines.append("")
    return "\n".join(lines)


def handle_admin_commands(posted, queue, state):
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
        elif command == "/queue":
            send_to_telegram(build_queue_message(queue), chat_id=TELEGRAM_ADMIN_CHAT_ID)
        elif command == "/sources":
            send_to_telegram(build_sources_message(), chat_id=TELEGRAM_ADMIN_CHAT_ID)
        elif command == "/pause":
            state["paused"] = True
            send_to_telegram("⏸ Публікацію в канал призупинено (черга далі наповнюється). /resume — щоб увімкнути назад.", chat_id=TELEGRAM_ADMIN_CHAT_ID)
        elif command == "/resume":
            state["paused"] = False
            send_to_telegram("▶️ Публікацію відновлено.", chat_id=TELEGRAM_ADMIN_CHAT_ID)
        elif command == "/help" or command == "/start":
            send_to_telegram(HELP_TEXT, chat_id=TELEGRAM_ADMIN_CHAT_ID)
        else:
            send_to_telegram("Не знаю такої команди.\n\n" + HELP_TEXT, chat_id=TELEGRAM_ADMIN_CHAT_ID)

    state["last_update_id"] = max_id
    return state


# ---------- main ----------

def enqueue_new_entries(queue, posted):
    """Fetch fresh RSS entries and add genuinely new ones to the queue."""
    data = fetch_recent_entries()
    queued_links = {it["link"] for items in queue.values() for it in items}

    for category, entries in data.items():
        new_entries = [e for e in entries if e["link"] not in posted and e["link"] not in queued_links]
        if not new_entries:
            continue
        selected = rank_and_summarize(category, new_entries, max_items=MAX_NEW_PER_CATEGORY_PER_RUN)
        if selected:
            queue.setdefault(category, []).extend(selected)
            print(f"[QUEUE] Added {len(selected)} new item(s) to '{category}'")
    return queue


def pop_next_from_queue(queue, state):
    """Round-robin across categories, FIFO within a category. Returns (category, item) or (None, None)."""
    n = len(CATEGORY_ORDER)
    start = state.get("rr_index", 0) % n
    for offset in range(n):
        idx = (start + offset) % n
        category = CATEGORY_ORDER[idx]
        items = queue.get(category, [])
        if items:
            item = items.pop(0)
            state["rr_index"] = (idx + 1) % n
            return category, item
    return None, None


def main():
    state = load_bot_state()
    posted = load_posted()
    queue = load_queue()

    state = handle_admin_commands(posted, queue, state)

    queue = enqueue_new_entries(queue, posted)
    save_queue(queue)

    if state.get("paused"):
        print("[INFO] Publishing is paused (via /pause). Queue updated, nothing posted.")
        save_bot_state(state)
        return

    last_publish_at = state.get("last_publish_at")
    now_utc = datetime.now(timezone.utc)
    due = True
    if last_publish_at:
        try:
            due = (now_utc - datetime.fromisoformat(last_publish_at)) >= PUBLISH_INTERVAL
        except ValueError:
            due = True

    if not due:
        print("[INFO] Not due yet (1 post/hour throttle). Queue updated, nothing posted.")
        save_bot_state(state)
        return

    category, item = pop_next_from_queue(queue, state)
    if not category:
        print("[INFO] Queue is empty, nothing to publish this run.")
        save_bot_state(state)
        return

    item = summarize_single(category, item)
    send_to_telegram(f"🌅 <b>{CHANNEL_NAME}</b>")
    time.sleep(1)
    send_to_telegram(format_single_message(category, item))
    print(f"[OK] Posted 1 item from '{category}': {item['title']}")

    posted[item["link"]] = {
        "title": item["title"],
        "category": category,
        "posted_at": datetime.now(KYIV_TZ).isoformat(),
    }
    state["last_publish_at"] = now_utc.isoformat()

    save_posted(posted)
    save_queue(queue)
    save_bot_state(state)


if __name__ == "__main__":
    main()
