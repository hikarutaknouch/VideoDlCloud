import os
import re
import asyncio
import subprocess
import shutil
from pathlib import Path

import discord
from discord.ext import commands

# -----------------------------------------------------------
# 1.  環境変数から機密情報を読み込む（ハードコード禁止）
# -----------------------------------------------------------
TOKEN  = os.environ["DISCORD_TOKEN"]               # Discord Bot トークン
CHANNEL = int(os.environ.get("TARGET_CHANNEL_ID", 0))  # 監視するチャンネル ID

# iCloud Drive に保存したい場合のみ有効（クラウド運用時は別ストレージに変更）
ICLOUD_DIR = os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/SocialVideos")

# yt‑dlp の実行パスを自動検出（Homebrew / apt など任意環境に対応）
YTDL = shutil.which("yt-dlp") or "/opt/homebrew/bin/yt-dlp"

# -----------------------------------------------------------
# 2.  ダウンロード対象 URL を判定する正規表現
#     Instagram / X(Twitter) / TikTok / YouTube(Shorts 含む)
# -----------------------------------------------------------
URL_RE = re.compile(
    r"https?://(?:www\.)?(?:instagram\.com|x\.com|twitter\.com|tiktok\.com|youtu\.be|youtube\.com)/(?:\S+)",
    re.IGNORECASE)

# -----------------------------------------------------------
# 3.  Discord Bot 初期化
# -----------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# -----------------------------------------------------------
# 4.  Cookie ファイル自動付与ヘルパー
# -----------------------------------------------------------
COOKIE_PATHS = {
    "instagram": Path.home() / ".config/yt-dlp/instagram_cookies.txt",
    "twitter":   Path.home() / ".config/yt-dlp/twitter_cookies.txt",
    "tiktok":    Path.home() / ".config/yt-dlp/tiktok_cookies.txt",
    "youtube":   Path.home() / ".config/yt-dlp/youtube_cookies.txt",
}

def cookie_for(url: str):
    if "instagram.com" in url:
        return COOKIE_PATHS["instagram"]
    if "twitter.com" in url or "x.com" in url:
        return COOKIE_PATHS["twitter"]
    if "tiktok.com" in url:
        return COOKIE_PATHS["tiktok"]
    if "youtube.com" in url or "youtu.be" in url:
        return COOKIE_PATHS["youtube"]
    return None

# -----------------------------------------------------------
# 5.  イベントハンドラ
# -----------------------------------------------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

@bot.event
async def on_message(msg):
    if msg.author.bot or (CHANNEL and msg.channel.id != CHANNEL):
        return

    for url in URL_RE.findall(msg.content):
        asyncio.create_task(download(url))

    await bot.process_commands(msg)

# -----------------------------------------------------------
# 6.  ダウンロード & 保存ロジック
# -----------------------------------------------------------
async def download(url: str):
    """指定 URL の動画を取得して iCloud (またはマウント先) に保存"""
    print(f"▶ START : {url}")

    out_tpl = os.path.join(ICLOUD_DIR, "%(uploader)s_%(id)s.%(ext)s")
    cmd = [
        YTDL,
        "-S", "vcodec:h264,acodec:m4a,ext:mp4  vp9/?av01/?*",  # QuickTime 互換優先 + フォールバック
        "--merge-output-format", "mp4",
        "-o", out_tpl,
        url,
    ]

    ck = cookie_for(url)
    if ck and ck.is_file():
        cmd += ["--cookies", str(ck)]

    proc = await asyncio.create_subprocess_exec(*cmd)
    await proc.wait()

    if proc.returncode == 0:
        print(f"✔ SAVED : {url}")
    else:
        print(f"✖ FAILED: {url} (rc={proc.returncode})")

# -----------------------------------------------------------
# 7.  エントリーポイント
# -----------------------------------------------------------
if __name__ == "__main__":
    bot.run(TOKEN)

