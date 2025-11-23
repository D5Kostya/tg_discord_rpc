"""
rpc_to_telegram.py

Discord → Telegram Music Profile синхронизация.

Зависимости:
pip install discord.py aiotdlib pydub mutagen requests pillow python-dotenv

Настройки:
- API_ID / API_HASH — от https://my.telegram.org
- DISCORD_TOKEN — токен вашего бота
- TARGET_DISCORD_USER_ID — ID пользователя для отслеживания (или None)
"""

import asyncio
import os
import time
from datetime import datetime, timezone

import discord
import requests
from PIL import Image, ImageDraw, ImageFont
from pydub import AudioSegment
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, APIC

from aiotdlib import Client
from aiotdlib.api import setProfileMusic, inputFileLocal

# ------------------ CONFIG ------------------
API_ID = 1234567
API_HASH = "your_api_hash"
TDLIB_DATABASE_DIR = "tdlib_data"

DISCORD_TOKEN = "your_discord_bot_token"
TARGET_DISCORD_USER_ID = None

UPDATE_INTERVAL = 60  # секунд
TMP_DIR = "./tmp_rpc"
os.makedirs(TMP_DIR, exist_ok=True)
ICON_CACHE_DIR = os.path.join(TMP_DIR, "icons")
os.makedirs(ICON_CACHE_DIR, exist_ok=True)
# --------------------------------------------

# ---------- TDLib client -------------------
client = Client(api_id=API_ID, api_hash=API_HASH, database_directory=TDLIB_DATABASE_DIR)

# ---------- Discord client -----------------
intents = discord.Intents.default()
intents.presences = True
intents.members = True
client_discord = discord.Client(intents=intents)

CURRENT_ACTIVITY = None
LAST_SENT_TITLE = None

# ---------- Helpers -----------------------
def generate_cover(text: str, out_path: str, size=(1000, 1000)) -> str:
    img = Image.new("RGB", size, (30, 30, 30))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 40)
    except Exception:
        font = ImageFont.load_default()

    max_width = size[0] - 40
    lines = []
    words = text.split()
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        wbox = draw.textbbox((0, 0), test, font=font)
        if wbox[2] <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)

    total_h = sum(draw.textbbox((0, 0), line, font=font)[3] - draw.textbbox((0, 0), line, font=font)[1] + 10 for line in lines)
    y = (size[1] - total_h) // 2
    for line in lines:
        wbox = draw.textbbox((0, 0), line, font=font)
        x = (size[0] - (wbox[2] - wbox[0])) // 2
        draw.text((x, y), line, font=font, fill=(240, 240, 240))
        y += (wbox[3] - wbox[1]) + 10

    img.save(out_path, format="PNG")
    return out_path

def download_icon_from_discord(application_id, icon_key):
    if not application_id or not icon_key:
        return None
    aid = icon_key.split('/')[-1]
    filename = os.path.join(ICON_CACHE_DIR, f"{application_id}_{aid}.png")
    if os.path.exists(filename):
        return filename
    url = f"https://cdn.discordapp.com/app-assets/{application_id}/{aid}.png"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            with open(filename, 'wb') as f:
                f.write(r.content)
            return filename
    except Exception:
        return None
    return None

def create_mp3_with_cover(title, artist, duration_seconds, cover_path, out_path):
    if duration_seconds < 1:
        duration_seconds = 1
    silence = AudioSegment.silent(duration=duration_seconds * 1000)
    silence.export(out_path, format="mp3")

    audio = MP3(out_path, ID3=ID3)
    try:
        audio.add_tags()
    except Exception:
        pass

    audio.tags.add(TIT2(encoding=3, text=title))
    audio.tags.add(TPE1(encoding=3, text=artist))

    if cover_path and os.path.exists(cover_path):
        with open(cover_path, 'rb') as imgf:
            imgdata = imgf.read()
        audio.tags.add(APIC(
            encoding=3,
            mime='image/png',
            type=3,
            desc='Cover',
            data=imgdata
        ))

    audio.save()
    return out_path

# ---------- TDLib helper -------------------
async def td_set_profile_music(file_path, title, performer, duration):
    if not client.is_running():
        await client.start()
    await client.invoke(setProfileMusic(
        file=inputFileLocal(path=file_path),
        title=title,
        performer=performer,
        duration=duration
    ))

# ---------- Discord event handlers ----------
@client_discord.event
async def on_ready():
    print(f"Discord bot connected as {client_discord.user}")

@client_discord.event
async def on_presence_update(before, after):
    global CURRENT_ACTIVITY
    if TARGET_DISCORD_USER_ID and after.id != TARGET_DISCORD_USER_ID:
        return

    act = None
    for a in after.activities:
        if getattr(a, 'name', None):
            act = a
            break
    if not act:
        CURRENT_ACTIVITY = None
        return

    title = getattr(act, 'name', '') or 'Discord Activity'
    details = getattr(act, 'details', '') or ''
    state = getattr(act, 'state', '') or ''

    ts = getattr(act, 'timestamps', None)
    start_ts = getattr(ts, 'start', None) if ts else None
    end_ts = getattr(ts, 'end', None) if ts else None
    now_ts = int(time.time())
    if isinstance(start_ts, datetime):
        start_ts = int(start_ts.replace(tzinfo=timezone.utc).timestamp())
    if isinstance(end_ts, datetime):
        end_ts = int(end_ts.replace(tzinfo=timezone.utc).timestamp())

    app_id = getattr(act, 'application_id', None)
    icon_key = getattr(act, 'large_image', None) or getattr(act, 'small_image', None)

    CURRENT_ACTIVITY = {
        'title': f"{title}" if not details else f"{title} — {details}",
        'artist': state or 'Discord',
        'application_id': app_id,
        'icon_key': icon_key,
        'start_ts': start_ts,
        'end_ts': end_ts,
        'raw': act
    }

# ---------- Main periodic worker --------------
async def periodic_worker():
    global LAST_SENT_TITLE
    while True:
        try:
            info = CURRENT_ACTIVITY
            if not info:
                await asyncio.sleep(UPDATE_INTERVAL)
                continue

            title = info.get('title') or 'Discord Activity'
            artist = info.get('artist') or 'Discord'

            # Проверяем, есть ли уже mp3 с таким названием
            safe_title = ''.join(c for c in title if c not in '/\\').strip()
            existing_files = [f for f in os.listdir(TMP_DIR) if f.endswith('.mp3') and safe_title in f]
            if existing_files:
                mp3_path = os.path.join(TMP_DIR, existing_files[-1])
                # Получаем длительность из существующего файла
                audio = MP3(mp3_path)
                duration = int(audio.info.length)
            else:
                duration = UPDATE_INTERVAL
                start_ts = info.get('start_ts')
                end_ts = info.get('end_ts')
                now_ts = int(time.time())
                if start_ts and end_ts:
                    duration = max(1, int(end_ts) - int(start_ts))
                elif start_ts and not end_ts:
                    duration = max(1, now_ts - int(start_ts))
                else:
                    duration = max(1, UPDATE_INTERVAL)

                cover_path = None
                app_id = info.get('application_id')
                icon_key = info.get('icon_key')
                if icon_key and app_id:
                    cover_path = download_icon_from_discord(app_id, icon_key)
                if not cover_path:
                    safe_name = ''.join(c for c in title if c.isalnum() or c in (' ', '-', '_')).strip()[:40]
                    cover_path = os.path.join(ICON_CACHE_DIR, f"generated_{safe_name}.png")
                    if not os.path.exists(cover_path):
                        generate_cover(title, cover_path)

                mp3_path = os.path.join(TMP_DIR, f"rpc_track_{int(time.time())}_{safe_title}.mp3")
                create_mp3_with_cover(safe_title, artist, duration, cover_path, mp3_path)

            print(f"[INFO] Setting profile music: '{title}'")
            await td_set_profile_music(mp3_path, safe_title, artist, duration)
            LAST_SENT_TITLE = title

        except Exception as e:
            print("[ERROR] periodic_worker:", e)

        await asyncio.sleep(UPDATE_INTERVAL)

# ---------- Entrypoint -------------------------
async def main():
    await client.start()
    loop = asyncio.get_event_loop()
    loop.create_task(client_discord.start(DISCORD_TOKEN))
    try:
        await periodic_worker()
    finally:
        await client.stop()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('Stopped by user')
