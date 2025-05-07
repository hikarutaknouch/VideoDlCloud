import os, re, asyncio, subprocess, tempfile, shutil
from pathlib import Path
import dropbox
import discord
from discord.ext import commands

TOKEN   = os.environ["DISCORD_TOKEN"]
CHANNEL = int(os.environ["TARGET_CHANNEL_ID"])
DROPBOX_TOKEN = os.environ["DROPBOX_TOKEN"]

YTDL = shutil.which("yt-dlp") or "/usr/local/bin/yt-dlp"
URL_RE = re.compile(r"https?://(?:www\.)?(?:instagram\.com|x\.com|twitter\.com|tiktok\.com|youtu\.be|youtube\.com)/\S+", re.I)

# cookie_for() と COOKIE_PATHS は省略（そのまま）

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot("!", intents=intents)

@bot.event
async def on_message(msg):
    if msg.author.bot or msg.channel.id != CHANNEL:
        return
    for url in URL_RE.findall(msg.content):
        asyncio.create_task(download(url))
    await bot.process_commands(msg)

async def download(url: str):
    print(f"▶ START : {url}")
    tmpdir = tempfile.mkdtemp()            # ← ここを追加
    try:
        out_tpl = os.path.join(tmpdir, "%(uploader)s_%(id)s.%(ext)s")

        cmd = [YTDL, "-S", "vcodec:h264,acodec:m4a,ext:mp4  vp9/?av01/?*",
               "--merge-output-format", "mp4", "-o", out_tpl, url]

        ck = cookie_for(url)
        if ck and ck.is_file():
            cmd += ["--cookies", str(ck)]

        if await asyncio.create_subprocess_exec(*cmd).wait() == 0:
            dbx = dropbox.Dropbox(DROPBOX_TOKEN)
            for fpath in Path(tmpdir).glob("*.mp4"):
                dpath = f"/SocialVideos/{fpath.name}"
                with open(fpath, "rb") as f:
                    dbx.files_upload(f.read(), dpath,
                                     mode=dropbox.files.WriteMode.overwrite)
                print(f"✔ Uploaded to Dropbox: {dpath}")
            print(f"✔ SAVED : {url}")
        else:
            print(f"✖ FAILED: {url}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

if __name__ == "__main__":
    bot.run(TOKEN)
