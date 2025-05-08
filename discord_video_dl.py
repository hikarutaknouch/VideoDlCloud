# discord_video_dl.py
import os, re, asyncio, tempfile, shutil, subprocess
from pathlib import Path
import discord
from discord.ext import commands

# --------------------------------------------------
# 1. 環境変数
# --------------------------------------------------
TOKEN          = os.environ["DISCORD_TOKEN"]
CHANNEL        = int(os.environ["TARGET_CHANNEL_ID"])        # 監視チャンネル ID

# --------------------------------------------------
# 2. 外部コマンドと正規表現
# --------------------------------------------------
YTDL = shutil.which("yt-dlp") or "/usr/local/bin/yt-dlp"
# 完全なURLパターンを抽出するように修正
URL_RE = re.compile(
    r"(https?://(?:www\.)?(?:instagram\.com|x\.com|twitter\.com|tiktok\.com|youtu\.be|youtube\.com)/\S+)",
    re.I)

# --------------------------------------------------
# 3. Cookie ファイルパス（必要なサービスだけ置けば OK）
# --------------------------------------------------
COOKIE_DIR = "/app/cookies"
Path(COOKIE_DIR).mkdir(exist_ok=True)

COOKIE_PATHS = {
    "instagram": Path(COOKIE_DIR) / "instagram_cookies.txt",
    "twitter":   Path(COOKIE_DIR) / "twitter_cookies.txt",
    "tiktok":    Path(COOKIE_DIR) / "tiktok_cookies.txt",
    "youtube":   Path(COOKIE_DIR) / "youtube_cookies.txt",
}

def cookie_for(url: str) -> Path | None:
    """URLからクッキーファイルのパスを取得"""
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
    
    urls = URL_RE.findall(msg.content)
    if urls:
        print(f"Found URLs: {urls}")
        for url in urls:
            # URLごとに非同期タスクとして処理
            asyncio.create_task(download_and_reply(url, msg.channel))
    
    await bot.process_commands(msg)

# --------------------------------------------------
# 6. ダウンロードとDiscordへの返信
# --------------------------------------------------
async def download_and_reply(url: str, channel):
    """URLから動画をダウンロードし、Discordチャンネルに直接送信する"""
    print(f"▶ START  : {url}")
    tmpdir = tempfile.mkdtemp()
    
    try:
        out_tpl = os.path.join(tmpdir, "%(uploader)s_%(id)s.%(ext)s")

        # yt-dlpのパスを確認
        ytdl_path = shutil.which("yt-dlp")
        print(f"Using yt-dlp from: {ytdl_path or YTDL}")

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
            print(f"Using cookie file: {ck}")
        else:
            print(f"No cookie file found for {url}")

        print(f"Running command: {' '.join(cmd)}")
        proc = await asyncio.create_subprocess_exec(*cmd)
        await proc.wait()

        if proc.returncode == 0:
            # ダウンロード成功
            # MP4ファイルを検索
            mp4_files = list(Path(tmpdir).glob("*.mp4"))
            print(f"Found {len(mp4_files)} MP4 files in {tmpdir}")
            
            if not mp4_files:
                await channel.send(f"❌ ダウンロードしたファイルが見つかりません: {url}")
                print(f"No MP4 files found in {tmpdir}. Directory contents:")
                for f in Path(tmpdir).iterdir():
                    print(f"- {f.name} ({f.stat().st_size} bytes)")
                return
            
            for mp4 in mp4_files:
                file_size = mp4.stat().st_size
                file_size_mb = file_size / (1024 * 1024)
                print(f"File size: {file_size_mb:.2f} MB")
                
                # Discordのファイルサイズ制限（無料: 8MB, Nitro: 50MB）
                discord_limit = 8 * 1024 * 1024  # 8MB (標準制限)
                
                if file_size <= discord_limit:
                    # Discordにファイルを直接送信
                    discord_file = discord.File(str(mp4))
                    await channel.send(f"✅ ダウンロード完了: {url}", file=discord_file)
                    print(f"✔ File sent to Discord: {mp4.name}")
                else:
                    # サイズが大きい場合は警告メッセージ
                    await channel.send(
                        f"⚠️ ファイルサイズが大きすぎます ({file_size_mb:.2f}MB)。"
                        f"Discord の制限は {discord_limit/(1024*1024)}MB です。"
                        f"別の方法でダウンロードするか、サイズを縮小してください。"
                    )
                    
                    # ファイルサイズを縮小するオプションを提供
                    await channel.send(
                        "画質を下げて再度ダウンロードするには、`!compress {url}` コマンドを使用してください。"
                    )
            
            print(f"✔ PROCESSED: {url}")
        else:
            # ダウンロード失敗
            await channel.send(f"❌ ダウンロード失敗: {url}")
            print(f"✖ FAILED : {url} (rc={proc.returncode})")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# --------------------------------------------------
# 7. 圧縮ダウンロードコマンド
# --------------------------------------------------
@bot.command(name="compress")
async def compress_download(ctx, url: str):
    """画質を下げて動画をダウンロードするコマンド"""
    if ctx.channel.id != CHANNEL:
        return
    
    await ctx.send(f"🔄 圧縮モードで処理中: {url}")
    
    tmpdir = tempfile.mkdtemp()
    try:
        out_tpl = os.path.join(tmpdir, "%(uploader)s_%(id)s.%(ext)s")
        
        # 低画質・低ビットレートでダウンロード
        cmd = [
            YTDL,
            "-S", "res:480,codec:h264",  # 480p解像度で十分
            "-f", "bestvideo[height<=480]+bestaudio/best[height<=480]",
            "--remux-video", "mp4",
            "-o", out_tpl,
            url,
        ]

        ck = cookie_for(url)
        if ck and ck.is_file():
            cmd += ["--cookies", str(ck)]
        
        proc = await asyncio.create_subprocess_exec(*cmd)
        await proc.wait()
        
        if proc.returncode == 0:
            mp4_files = list(Path(tmpdir).glob("*.mp4"))
            
            if not mp4_files:
                await ctx.send(f"❌ 圧縮ダウンロードに失敗しました: {url}")
                return
            
            for mp4 in mp4_files:
                file_size = mp4.stat().st_size
                file_size_mb = file_size / (1024 * 1024)
                
                discord_limit = 8 * 1024 * 1024  # 8MB
                
                if file_size <= discord_limit:
                    discord_file = discord.File(str(mp4))
                    await ctx.send(
                        f"✅ 圧縮ダウンロード完了 ({file_size_mb:.2f}MB): {url}", 
                        file=discord_file
                    )
                else:
                    # それでも大きい場合
                    await ctx.send(
                        f"⚠️ 圧縮してもファイルサイズが大きすぎます ({file_size_mb:.2f}MB)。"
                    )
        else:
            await ctx.send(f"❌ 圧縮ダウンロードに失敗しました: {url}")
    
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# --------------------------------------------------
# 8. エントリーポイント
# --------------------------------------------------
if __name__ == "__main__":
    bot.run(TOKEN)