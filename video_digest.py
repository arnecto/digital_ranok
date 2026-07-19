#!/usr/bin/env python3
"""
Twice-a-day video digest.

Looks at everything published to the Telegram channel (posted_links.json,
written by news_bot.py) since the last run, asks Gemini which single item
is most worth turning into a short video, generates that video
(make_video.py), and sends it to the admin in a private Telegram chat for
manual posting to TikTok.

This is the "semi-automatic" bridge: once the TikTok Content Posting API
app is approved, the send-to-admin step can be swapped for a direct
TikTok upload.

Environment variables required:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_ADMIN_CHAT_ID
    GEMINI_API_KEY       - to rank which news item is most video-worthy
    PEXELS_API_KEY       - for background footage (see make_video.py)

Schedule (cron, twice a day — 12:00 and 00:00 Kyiv time):
    0 9,21 * * *   (UTC; Kyiv is UTC+3 in summer, adjust if needed)
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_video import (
    generate_narration, get_audio_duration, get_background_clip,
    render_text_overlay, compose_video, CHANNEL_OUTRO,
)
import tempfile

# ---------- CONFIG ----------

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-flash-lite-latest"

POSTED_FILE = "posted_links.json"
VIDEO_STATE_FILE = "video_state.json"
WINDOW_HOURS = 12  # matches the "12:00 / 00:00" cadence

KYIV_TZ = ZoneInfo("Europe/Kyiv")


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


def get_candidates(posted, since_iso):
    since = datetime.fromisoformat(since_iso)
    candidates = []
    for link, info in posted.items():
        try:
            posted_at = datetime.fromisoformat(info["posted_at"])
        except (KeyError, ValueError):
            continue
        if posted_at >= since and info.get("summary"):
            candidates.append(info)
    return candidates


def pick_best(candidates):
    """Ask Gemini which single item is most worth a video. Falls back to the newest one."""
    if len(candidates) == 1:
        return candidates[0]
    if not GEMINI_API_KEY:
        return max(candidates, key=lambda c: c["posted_at"])

    items_text = "\n\n".join(
        f"[{i}] {c['title']} ({c['category']})\n{c['summary']}"
        for i, c in enumerate(candidates)
    )
    prompt = (
        "Ось новини, опубліковані за останні 12 годин. Обери ОДНУ, "
        "найбільш варту короткого відео для TikTok (найбільш вірусну, "
        "зрозумілу без контексту, з чітким 'гачком').\n"
        'Поверни ЛИШЕ JSON формату {"index": <номер>}, без іншого тексту.\n\n'
        f"{items_text}"
    )
    try:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=60,
        )
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        idx = json.loads(text)["index"]
        return candidates[idx]
    except (requests.exceptions.RequestException, KeyError, IndexError, ValueError, json.JSONDecodeError) as e:
        print(f"[WARN] Gemini ranking failed: {e}. Falling back to newest item.")
        return max(candidates, key=lambda c: c["posted_at"])


def send_video_to_telegram(video_path, caption):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
    with open(video_path, "rb") as f:
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_ADMIN_CHAT_ID, "caption": caption, "parse_mode": "HTML"},
            files={"video": f},
            timeout=120,
        )
    resp.raise_for_status()


def send_text_to_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url, data={"chat_id": TELEGRAM_ADMIN_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=30,
    )
    resp.raise_for_status()


def main():
    if not TELEGRAM_ADMIN_CHAT_ID:
        print("[ERROR] TELEGRAM_ADMIN_CHAT_ID not set, nowhere to send the video.")
        return

    state = load_json(VIDEO_STATE_FILE, {})
    now = datetime.now(timezone.utc)
    since_iso = state.get("last_run_at") or (now - timedelta(hours=WINDOW_HOURS)).isoformat()

    posted = load_json(POSTED_FILE, {})
    candidates = get_candidates(posted, since_iso)

    if not candidates:
        with_summary = sum(1 for v in posted.values() if v.get("summary"))
        print("[INFO] No eligible news items in the window, skipping video generation.")
        print(f"[INFO] Window start was: {since_iso}")
        print(f"[INFO] Total posted_links.json entries: {len(posted)}, with 'summary' field: {with_summary}")
        return

    best = pick_best(candidates)
    print(f"[INFO] Selected for video: {best['title']}")

    narration_text = f"{best['title']}. {best['summary']} {CHANNEL_OUTRO}"

    with tempfile.TemporaryDirectory() as workdir:
        audio_path = os.path.join(workdir, "narration.mp3")
        generate_narration(narration_text, audio_path)
        duration = get_audio_duration(audio_path)

        background_path = get_background_clip(
            best.get("video_keywords", ""), best.get("category", ""), workdir
        )

        overlay_path = os.path.join(workdir, "overlay.png")
        render_text_overlay(narration_text, overlay_path)

        out_path = os.path.join(workdir, "digest_video.mp4")
        compose_video(background_path, overlay_path, audio_path, out_path, duration)

        caption = (
            f"🎬 <b>Відео готове</b>\n\n<b>{best['title']}</b>\n"
            f"Категорія: {best['category']}\n\n"
            f"Перевір і опублікуй у TikTok вручну (Direct Post ще не активний)."
        )
        send_video_to_telegram(out_path, caption)
        print("[OK] Video sent to admin.")

    state["last_run_at"] = now.isoformat()
    save_json(VIDEO_STATE_FILE, state)


if __name__ == "__main__":
    main()
