#!/usr/bin/env python3
"""
Generates a short vertical (1080x1920) news video "Daily Mail style":
a relevant background video clip (via Pexels), a semi-transparent text
box with the news summary, Ukrainian voiceover (edge-tts), and a short
channel plug at the end.

Usage:
    python make_video.py --title "..." --summary "..." --keywords "courtroom, lawsuit" \
        --category "IT / AI" --out output_video.mp4

Environment variables required:
    PEXELS_API_KEY - free key from pexels.com/api

Dependencies (see requirements.txt):
    edge-tts, requests, pillow
    + system ffmpeg/ffprobe (present on ubuntu-latest GitHub runners)
"""

import argparse
import asyncio
import json
import os
import random
import subprocess
import sys
import tempfile

import requests
from PIL import Image, ImageDraw, ImageFont

# ---------- CONFIG ----------

CANVAS_W, CANVAS_H = 1080, 1920
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

BOX_BG = (10, 8, 20, 210)          # semi-transparent near-black
BOX_ACCENT = (255, 138, 61, 255)   # brand orange (matches site/logo)
TEXT_COLOR = (255, 255, 255, 255)

CHANNEL_OUTRO = "А більше новин ви знайдете в телеграм-каналі Цифровий Ранок."

PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY")
TTS_VOICE = "uk-UA-PolinaNeural"  # also available: uk-UA-OstapNeural (male)

FALLBACK_QUERY_BY_CATEGORY = {
    "IT / AI": "technology computer",
    "Бізнес / Стартапи": "business office city",
    "Крипта": "cryptocurrency blockchain coins",
}


# ---------- TTS ----------

def generate_narration(text, out_path):
    """Generate Ukrainian voiceover with edge-tts (free, no API key)."""
    import edge_tts

    async def _run():
        communicate = edge_tts.Communicate(text, TTS_VOICE)
        await communicate.save(out_path)

    asyncio.run(_run())


def get_audio_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "json", path],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


# ---------- BACKGROUND VIDEO ----------

def search_pexels_video(query, min_duration=5):
    """Return a direct .mp4 URL for a vertical-ish clip matching the query."""
    if not PEXELS_API_KEY:
        return None
    resp = requests.get(
        "https://api.pexels.com/videos/search",
        headers={"Authorization": PEXELS_API_KEY},
        params={"query": query, "orientation": "portrait", "per_page": 6},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    videos = [v for v in data.get("videos", []) if v.get("duration", 0) >= min_duration]
    if not videos:
        return None

    random.shuffle(videos)
    video = videos[0]
    # pick the highest-resolution portrait-ish file available
    files = sorted(
        video["video_files"],
        key=lambda f: (f.get("height") or 0),
        reverse=True,
    )
    for f in files:
        if f.get("file_type") == "video/mp4":
            return f["link"]
    return files[0]["link"] if files else None


def download_file(url, out_path):
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    with open(out_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1 << 16):
            f.write(chunk)


def get_background_clip(keywords, category, workdir):
    """Try the AI-suggested keywords first, then fall back to a category-level query."""
    bg_path = os.path.join(workdir, "background.mp4")

    for query in filter(None, [keywords, FALLBACK_QUERY_BY_CATEGORY.get(category, "technology")]):
        try:
            url = search_pexels_video(query)
            if url:
                print(f"[INFO] Using Pexels clip for query: '{query}'")
                download_file(url, bg_path)
                return bg_path
        except requests.exceptions.RequestException as e:
            print(f"[WARN] Pexels search failed for '{query}': {e}")

    print("[WARN] No Pexels clip found, generating a plain animated fallback background.")
    return generate_fallback_background(workdir)


def generate_fallback_background(workdir, duration=25):
    """Procedural animated gradient as a last-resort background (no external assets needed)."""
    bg_path = os.path.join(workdir, "background.mp4")
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"color=c=0x1a1442:s={CANVAS_W}x{CANVAS_H}:d={duration},"
              f"noise=alls=8:allf=t+u",
        "-vf", "hue=H=2*PI*t/20:s=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", bg_path,
    ], check=True, capture_output=True)
    return bg_path


# ---------- TEXT OVERLAY ----------

def wrap_text(draw, text, font, max_width):
    words = text.split()
    lines, current = [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def render_text_overlay(text, out_path):
    """Render the semi-transparent text box (same visual language as the reference video)."""
    img = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    max_text_width = CANVAS_W - 160
    max_box_height = int(CANVAS_H * 0.6)  # never take up more than 60% of the screen

    font_size = 42
    while font_size >= 26:
        font = ImageFont.truetype(FONT_BOLD, font_size)
        lines = wrap_text(draw, text, font, max_text_width)
        ascent, descent = font.getmetrics()
        line_height = int((ascent + descent) * 1.35)  # proper spacing, no overlap
        text_block_height = line_height * len(lines)
        padding_y = 50
        box_height = text_block_height + padding_y * 2
        if box_height <= max_box_height:
            break
        font_size -= 4
    else:
        box_height = min(box_height, max_box_height)

    box_top = (CANVAS_H - box_height) // 2
    box_left = 40
    box_right = CANVAS_W - 40
    corner_radius = 28

    # semi-transparent rounded box
    draw.rounded_rectangle(
        [box_left, box_top, box_right, box_top + box_height],
        radius=corner_radius, fill=BOX_BG,
    )
    # accent stripe on the left, inset vertically so it doesn't poke out of the rounded corners
    stripe_inset = corner_radius
    draw.rounded_rectangle(
        [box_left, box_top + stripe_inset, box_left + 8, box_top + box_height - stripe_inset],
        radius=4, fill=BOX_ACCENT,
    )

    y = box_top + padding_y + (line_height - (ascent + descent)) // 2
    for line in lines:
        w = draw.textlength(line, font=font)
        x = (CANVAS_W - w) // 2
        draw.text((x, y), line, font=font, fill=TEXT_COLOR)
        y += line_height

    img.save(out_path)
    return box_top, box_height


# ---------- COMPOSE ----------

def compose_video(background_path, overlay_path, audio_path, out_path, duration):
    """Loop/trim background to `duration`, scale/crop to 1080x1920, burn in overlay + audio."""
    filter_complex = (
        f"[0:v]scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=increase,"
        f"crop={CANVAS_W}:{CANVAS_H},setsar=1[bg];"
        f"[bg][1:v]overlay=0:0:format=auto,"
        f"fade=t=in:st=0:d=0.5,fade=t=out:st={max(duration-0.6,0)}:d=0.6[v]"
    )
    subprocess.run([
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", background_path,
        "-i", overlay_path,
        "-i", audio_path,
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "2:a",
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        out_path,
    ], check=True)


# ---------- MAIN ----------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--keywords", default="")
    parser.add_argument("--category", default="")
    parser.add_argument("--out", default="output_video.mp4")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as workdir:
        narration_text = f"{args.title}. {args.summary} {CHANNEL_OUTRO}"
        display_text = f"{args.title}. {args.summary} {CHANNEL_OUTRO}"

        print("[INFO] Generating voiceover...")
        audio_path = os.path.join(workdir, "narration.mp3")
        generate_narration(narration_text, audio_path)
        duration = get_audio_duration(audio_path)
        print(f"[INFO] Narration duration: {duration:.1f}s")

        print("[INFO] Fetching background clip...")
        background_path = get_background_clip(args.keywords, args.category, workdir)

        print("[INFO] Rendering text overlay...")
        overlay_path = os.path.join(workdir, "overlay.png")
        render_text_overlay(display_text, overlay_path)

        print("[INFO] Composing final video...")
        compose_video(background_path, overlay_path, audio_path, args.out, duration)

        print(f"[OK] Saved: {args.out}")


if __name__ == "__main__":
    main()
