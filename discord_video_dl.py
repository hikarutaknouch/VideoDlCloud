# discord_video_dl.py
import os, re, asyncio, tempfile, shutil, subprocess, requests
from pathlib import Path
import discord
from discord.ext import commands
from urllib.parse import urlparse

# --------------------------------------------------
# 1. 環境変数
# --------------------------------------------------
TOKEN          = os.environ["DISCORD_TOKEN"]
CHANNEL        = int(os.environ["TARGET_CHANNEL_ID"])        # 監視チャンネル ID

# --------------------------------------------------
# 2. 外部コマンドと正規表現
# --------------------------------------------------
YTDL = shutil.which("yt-dlp") or "/usr/local/bin/yt-dlp"
# 完全なURLパターンを抽出するように修正（画像URLも含む）
URL_RE = re.compile(
    r"(https?://(?:www\.)?(?:instagram\.com|x\.com|twitter\.com|tiktok\.com|youtu\.be|youtube\.com)/\S+)",
    re.I)

# 画像URLを判定する正規表現（拡張子ベース）
IMAGE_RE = re.compile(
    r"(https?://\S+\.(?:jpg|jpeg|png|gif|webp)(?:\?\S*)?$)", 
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
    
    # まず、メッセージ内のすべてのURLを取得
    all_urls = re.findall(r'(https?://\S+)', msg.content)
    
    if all_urls:
        print(f"Found URLs: {all_urls}")
        
        for url in all_urls:
            # 画像URLかどうかを判定
            if is_image_url(url):
                # 画像URLの場合
                asyncio.create_task(download_image_and_reply(url, msg.channel))
            elif URL_RE.match(url):
                # 動画URLの場合（Instagram, Twitter, TikTok, YouTubeなど）
                asyncio.create_task(download_and_reply(url, msg.channel))
    
    await bot.process_commands(msg)

def is_image_url(url: str) -> bool:
    """URLが画像URLかどうかを判定する"""
    # 拡張子による判定
    if IMAGE_RE.match(url):
        return True
    
    # Twitterの画像URLは特殊なフォーマットを持つことがある
    if ('pbs.twimg.com' in url or 'twitter.com' in url or 'x.com' in url) and ('media' in url or 'photo' in url):
        return True
        
    # InstagramやFacebookの画像URLも特殊
    if ('instagram.com' in url or 'facebook.com' in url) and ('p/' in url or 'photo' in url):
        # 画像投稿の可能性が高いが、実際には動画かもしれないので
        # 両方のダウンローダーを試す方がいいかもしれない
        return False
    
    return False

# --------------------------------------------------
# 6. 画像ダウンロード関数
# --------------------------------------------------
async def download_image_and_reply(url: str, channel):
    """URLから画像をダウンロードし、Discordチャンネルに直接送信する"""
    print(f"▶ START IMAGE DOWNLOAD : {url}")
    
    # 一時ディレクトリを作成
    tmpdir = tempfile.mkdtemp()
    
    try:
        # URLからファイル名を取得
        parsed_url = urlparse(url)
        path = parsed_url.path
        filename = os.path.basename(path)
        
        # ファイル名が不適切な場合はデフォルト名を設定
        if not filename or '.' not in filename:
            filename = f"image_{int(asyncio.get_event_loop().time())}.jpg"
        
        # ファイルパス
        file_path = os.path.join(tmpdir, filename)
        
        # 画像をダウンロード
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        # Cookieが必要なサイトの場合
        cookies = None
        ck = cookie_for(url)
        if ck and ck.is_file():
            # Cookieファイルを読み込む（単純化のため実装省略）
            print(f"Using cookie file: {ck}")
        
        # リクエスト送信
        response = requests.get(url, headers=headers, cookies=cookies, stream=True)
        
        if response.status_code == 200:
            # 画像を保存
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1024):
                    if chunk:
                        f.write(chunk)
            
            # ファイルサイズを確認
            file_size = os.path.getsize(file_path)
            file_size_mb = file_size / (1024 * 1024)
            print(f"Image downloaded: {filename} ({file_size_mb:.2f} MB)")
            
            # Discordのファイルサイズ制限（無料: 8MB）
            discord_limit = 8 * 1024 * 1024
            
            if file_size <= discord_limit:
                # Discordにファイルを直接送信
                discord_file = discord.File(file_path)
                await channel.send(f"✅ 画像ダウンロード完了: {url}", file=discord_file)
                print(f"✔ Image sent to Discord: {filename}")
            else:
                # サイズが大きすぎる場合
                await channel.send(
                    f"⚠️ 画像ファイルサイズが大きすぎます ({file_size_mb:.2f}MB)。"
                    f"Discord の制限は {discord_limit/(1024*1024)}MB です。"
                )
        else:
            # ダウンロード失敗
            await channel.send(f"❌ 画像ダウンロード失敗: {url} (ステータスコード: {response.status_code})")
            print(f"✖ IMAGE DOWNLOAD FAILED: {url} (status={response.status_code})")
    
    except Exception as e:
        # エラー処理
        await channel.send(f"❌ 画像ダウンロード中にエラーが発生しました: {url}")
        print(f"✖ IMAGE DOWNLOAD ERROR: {url} - {str(e)}")
    
    finally:
        # 一時ディレクトリを削除
        shutil.rmtree(tmpdir, ignore_errors=True)

# --------------------------------------------------
# 7. 動画ダウンロード関数
# --------------------------------------------------
async def download_and_reply(url: str, channel):
    """URLから動画をダウンロードし、Discordチャンネルに直接送信する"""
    print(f"▶ START VIDEO DOWNLOAD: {url}")
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
                # MP4が見つからない場合は画像ファイルを確認
                image_files = list(Path(tmpdir).glob("*.jpg")) + list(Path(tmpdir).glob("*.jpeg")) + list(Path(tmpdir).glob("*.png")) + list(Path(tmpdir).glob("*.gif"))
                
                if image_files:
                    print(f"Found {len(image_files)} image files in {tmpdir}")
                    for img in image_files:
                        file_size = img.stat().st_size
                        file_size_mb = file_size / (1024 * 1024)
                        
                        discord_limit = 8 * 1024 * 1024
                        
                        if file_size <= discord_limit:
                            discord_file = discord.File(str(img))
                            await channel.send(f"✅ 画像ダウンロード完了: {url}", file=discord_file)
                            print(f"✔ Image sent to Discord: {img.name}")
                        else:
                            await channel.send(
                                f"⚠️ 画像ファイルサイズが大きすぎます ({file_size_mb:.2f}MB)。"
                                f"Discord の制限は {discord_limit/(1024*1024)}MB です。"
                            )
                    return
                
                # それでもファイルが見つからない場合
                await channel.send(f"❌ ダウンロードしたファイルが見つかりません: {url}")
                print(f"No files found in {tmpdir}. Directory contents:")
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
                        f"画質を下げて再度ダウンロードするには、`!compress {url}` コマンドを使用してください。"
                    )
            
            print(f"✔ PROCESSED: {url}")
        else:
            # ダウンロード失敗
            await channel.send(f"❌ ダウンロード失敗: {url}")
            print(f"✖ FAILED : {url} (rc={proc.returncode})")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# --------------------------------------------------
# 8. 圧縮ダウンロードコマンド
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
# 9. 画像ダウンロードコマンド
# --------------------------------------------------
@bot.command(name="image")
async def image_download(ctx, url: str):
    """画像を明示的にダウンロードするコマンド"""
    if ctx.channel.id != CHANNEL:
        return
    
    await ctx.send(f"🔄 画像のダウンロード中: {url}")
    await download_image_and_reply(url, ctx.channel)

# --------------------------------------------------
# 10. エントリーポイント
# --------------------------------------------------
if __name__ == "__main__":
    bot.run(TOKEN)