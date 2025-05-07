# discord_video_dl.py
import os, re, asyncio, tempfile, shutil, subprocess
from pathlib import Path
import dropbox
import discord
from discord.ext import commands

# --------------------------------------------------
# 1. 環境変数
# --------------------------------------------------
TOKEN          = os.environ["DISCORD_TOKEN"]
CHANNEL        = int(os.environ["TARGET_CHANNEL_ID"])        # 監視チャンネル ID
DROPBOX_TOKEN  = os.environ["DROPBOX_TOKEN"]

# --------------------------------------------------
# 2. 外部コマンドと正規表現
# --------------------------------------------------
YTDL = shutil.which("yt-dlp") or "/usr/local/bin/yt-dlp"
URL_RE = re.compile(
    r"https?://(?:www\.)?"
    r"(instagram\.com|x\.com|twitter\.com|tiktok\.com|youtu\.be|youtube\.com)/\S+",
    re.I)

# --------------------------------------------------
# 3. Cookie ファイルパス（必要なサービスだけ置けば OK）
#    ブラウザ拡張「cookies.txt」でエクスポート
# --------------------------------------------------
COOKIE_PATHS = {
    "instagram": Path.home() / ".config/yt-dlp/instagram_cookies.txt",
    "twitter":   Path.home() / ".config/yt-dlp/twitter_cookies.txt",
    "tiktok":    Path.home() / ".config/yt-dlp/tiktok_cookies.txt",
    "youtube":   Path.home() / ".config/yt-dlp/youtube_cookies.txt",
}

def cookie_for(url: str) -> Path | None:
    if "instagram.com" in url:
        return COOKIE_PATHS["instagram"]
    if "twitter.com" in url or "x.com" in url:
        return COOKIE_PATHS["twitter"]
    if "tiktok.com" in url:
        return COOKIE_PATHS["tiktok"]
    if "youtube.com" in url or "youtu.be" in url:
        return COOKIE_PATHS["youtube"]
    return None

# --------------------------------------------------
# 4. Discord Bot 初期化
# --------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --------------------------------------------------
# 5. メッセージ受信ハンドラ
# --------------------------------------------------
@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot or msg.channel.id != CHANNEL:
        return
    for url in URL_RE.findall(msg.content):
        asyncio.create_task(download(url))
    await bot.process_commands(msg)

# --------------------------------------------------
# 6. ダウンロード + Dropbox アップロード
# --------------------------------------------------
async def download(url: str):
    print(f"▶ START  : {url}")
    tmpdir = tempfile.mkdtemp()         # ---- 修正① ここで必ず生成 ----
    try:
        out_tpl = os.path.join(tmpdir, "%(uploader)s_%(id)s.%(ext)s")

        # yt-dlp コマンド
        cmd = [
            YTDL,
            "-S", "vcodec:h264,acodec:m4a,ext:mp4  vp9/?av01/?*",  # QuickTime 互換優先
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
            dbx = dropbox.Dropbox(DROPBOX_TOKEN)
            for mp4 in Path(tmpdir).glob("*.mp4"):
                dpath = f"/SocialVideos/{mp4.name}"
                with open(mp4, "rb") as f:
                    dbx.files_upload(
                        f.read(), dpath,
                        mode=dropbox.files.WriteMode.overwrite)
                print(f"✔ Uploaded to Dropbox: {dpath}")
            print(f"✔ SAVED  : {url}")
        else:
            print(f"✖ FAILED : {url} (rc={proc.returncode})")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)  # ---- 後始末 ----

# --------------------------------------------------
# 7. エントリーポイント
# --------------------------------------------------
if __name__ == "__main__":
    bot.run(TOKEN)
