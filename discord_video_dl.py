# discord_video_dl.py
import os, re, asyncio, tempfile, shutil, subprocess, requests, json
from pathlib import Path
import discord
from discord.ext import commands
from urllib.parse import urlparse, urljoin
import random
import time

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

# Instagramの投稿URLパターン
INSTAGRAM_POST_RE = re.compile(
    r"(https?://(?:www\.)?instagram\.com/p/([^/?]+))",
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
            # Instagramの投稿URLかどうかを確認
            instagram_match = INSTAGRAM_POST_RE.match(url)
            if instagram_match:
                # Instagramの投稿URLは専用の関数で処理
                asyncio.create_task(download_instagram_post(url, msg.channel))
            # 画像URLかどうかを判定
            elif is_image_url(url):
                # 画像URLの場合
                asyncio.create_task(download_image_and_reply(url, msg.channel))
            elif URL_RE.match(url):
                # 動画URLの場合（Twitter/X, TikTok, YouTubeなど）
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
    
    return False

# --------------------------------------------------
# 6. Instagram投稿ダウンロード関数
# --------------------------------------------------
async def download_instagram_post(url: str, channel):
    """InstagramのURLから画像または動画をダウンロードする"""
    print(f"▶ START INSTAGRAM DOWNLOAD: {url}")
    tmpdir = tempfile.mkdtemp()
    
    try:
        # まず動画として処理を試みる
        print("Trying to download as video first...")
        video_success = await download_with_ytdlp(url, tmpdir, channel)
        
        if not video_success:
            print("No video found, trying to download images...")
            # 動画がない場合、画像の取得を試みる
            
            # Instagram APIを使用して画像URLを取得する方法
            # (直接画像URLを取得するには通常認証が必要です)
            # ここではyt-dlpのjson出力を使って試みます
            
            # yt-dlpでJSONメタデータの取得
            json_file = os.path.join(tmpdir, "info.json")
            cmd = [
                YTDL,
                "--dump-json",
                "--no-check-certificates",
                "-o", os.path.join(tmpdir, "%(title)s.%(ext)s"),
                url
            ]
            
            ck = cookie_for(url)
            if ck and ck.is_file():
                cmd += ["--cookies", str(ck)]
            
            try:
                # JSONメタデータの取得を試みる
                proc = await asyncio.create_subprocess_exec(
                    *cmd, 
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await proc.communicate()
                
                if proc.returncode == 0 and stdout:
                    # JSONデータの解析
                    try:
                        data = json.loads(stdout)
                        if 'thumbnails' in data and data['thumbnails']:
                            # サムネイル画像のURLを取得
                            best_thumbnail = max(data['thumbnails'], key=lambda x: x.get('width', 0) if x.get('width') else 0)
                            thumbnail_url = best_thumbnail.get('url')
                            
                            if thumbnail_url:
                                print(f"Found thumbnail URL: {thumbnail_url}")
                                await download_image_and_reply(thumbnail_url, channel, custom_message=f"✅ Instagram画像をダウンロードしました: {url}")
                                return True
                    except json.JSONDecodeError:
                        print("Failed to parse JSON data")
                
                print(f"yt-dlp stderr: {stderr.decode('utf-8', errors='ignore')}")
            except Exception as e:
                print(f"Error getting JSON metadata: {e}")
            
            # ウェブスクレイピングで画像を取得する代替方法
            # (この方法は不安定で、Instagram側の変更により動作しなくなる可能性があります)
            try:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.5',
                    'Connection': 'keep-alive',
                }
                
                response = requests.get(url, headers=headers)
                if response.status_code == 200:
                    html_content = response.text
                    
                    # メタデータを探す
                    image_urls = []
                    
                    # og:image メタタグを探す
                    og_image_match = re.search(r'<meta property="og:image" content="([^"]+)"', html_content)
                    if og_image_match:
                        image_urls.append(og_image_match.group(1))
                    
                    # JSON LDデータを探す
                    json_ld_match = re.search(r'<script type="application/ld\+json">(.+?)</script>', html_content, re.DOTALL)
                    if json_ld_match:
                        try:
                            json_data = json.loads(json_ld_match.group(1))
                            if 'image' in json_data:
                                if isinstance(json_data['image'], list):
                                    image_urls.extend(json_data['image'])
                                else:
                                    image_urls.append(json_data['image'])
                        except json.JSONDecodeError:
                            pass
                    
                    # 画像URLを含むJSONデータを探す
                    json_data_match = re.search(r'"display_url":"([^"]+)"', html_content)
                    if json_data_match:
                        image_url = json_data_match.group(1).replace('\\u0026', '&')
                        image_urls.append(image_url)
                    
                    if image_urls:
                        # 最初の画像URLを使用
                        image_url = image_urls[0]
                        print(f"Found image URL from HTML: {image_url}")
                        await download_image_and_reply(image_url, channel, custom_message=f"✅ Instagram画像をダウンロードしました: {url}")
                        return True
                    else:
                        print("No image URLs found in HTML")
            except Exception as e:
                print(f"Error scraping HTML: {e}")
            
            # すべての方法が失敗した場合はエラーメッセージを送信
            await channel.send(f"❌ Instagramの画像/動画の取得に失敗しました: {url}")
            print(f"❌ Failed to download Instagram content: {url}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    
    return False

async def download_with_ytdlp(url, tmpdir, channel):
    """yt-dlpを使用してメディアをダウンロードする"""
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
                return True
            
            # ファイルが見つからない場合
            print(f"No media files found in {tmpdir}. Directory contents:")
            for f in Path(tmpdir).iterdir():
                print(f"- {f.name} ({f.stat().st_size} bytes)")
            return False
        
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
        return True
    else:
        # ダウンロード失敗
        print(f"✖ FAILED with yt-dlp: {url} (rc={proc.returncode})")
        return False

# --------------------------------------------------
# 7. 画像ダウンロード関数
# --------------------------------------------------
async def download_image_and_reply(url: str, channel, custom_message=None):
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
            filename = f"image_{int(time.time())}_{random.randint(1000, 9999)}.jpg"
        
        # ファイルパス
        file_path = os.path.join(tmpdir, filename)
        
        # 画像をダウンロード
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Referer': 'https://www.instagram.com/',  # Instagramからの参照を装う
        }
        
        # Cookieが必要なサイトの場合
        cookies = None
        ck = cookie_for(url)
        if ck and ck.is_file():
            print(f"Using cookie file: {ck}")
            # 実際のCookieファイルの読み込みはここに実装
        
        # リクエスト送信
        response = requests.get(url, headers=headers, cookies=cookies, stream=True, timeout=30)
        
        if response.status_code == 200:
            content_type = response.headers.get('Content-Type', '')
            if 'image' in content_type or 'octet-stream' in content_type:
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
                    message = custom_message if custom_message else f"✅ 画像ダウンロード完了: {url}"
                    await channel.send(message, file=discord_file)
                    print(f"✔ Image sent to Discord: {filename}")
                    return True
                else:
                    # サイズが大きすぎる場合
                    await channel.send(
                        f"⚠️ 画像ファイルサイズが大きすぎます ({file_size_mb:.2f}MB)。"
                        f"Discord の制限は {discord_limit/(1024*1024)}MB です。"
                    )
            else:
                await channel.send(f"❌ URLから画像を取得できませんでした: {url} (Content-Type: {content_type})")
                print(f"Not an image: Content-Type is {content_type}")
        else:
            # ダウンロード失敗
            await channel.send(f"❌ 画像ダウンロード失敗: {url} (ステータスコード: {response.status_code})")
            print(f"✖ IMAGE DOWNLOAD FAILED: {url} (status={response.status_code})")
    
    except Exception as e:
        # エラー処理
        await channel.send(f"❌ 画像ダウンロード中にエラーが発生しました: {url}")
        print(f"✖ IMAGE DOWNLOAD ERROR: {url} - {str(e)}")
        return False
    
    finally:
        # 一時ディレクトリを削除
        shutil.rmtree(tmpdir, ignore_errors=True)
    
    return False

# --------------------------------------------------
# 8. 動画ダウンロード関数
# --------------------------------------------------
async def download_and_reply(url: str, channel):
    """URLから動画をダウンロードし、Discordチャンネルに直接送信する"""
    print(f"▶ START VIDEO DOWNLOAD: {url}")
    tmpdir = tempfile.mkdtemp()
    
    try:
        await download_with_ytdlp(url, tmpdir, channel)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# --------------------------------------------------
# 9. 圧縮ダウンロードコマンド
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
# 10. 画像ダウンロードコマンド
# --------------------------------------------------
@bot.command(name="image")
async def image_download(ctx, url: str):
    """画像を明示的にダウンロードするコマンド"""
    if ctx.channel.id != CHANNEL:
        return
    
    await ctx.send(f"🔄 画像のダウンロード中: {url}")
    await download_image_and_reply(url, ctx.channel)

# --------------------------------------------------
# 11. Instagram専用コマンド
# --------------------------------------------------
@bot.command(name="instagram")
async def instagram_download(ctx, url: str):
    """Instagramの投稿をダウンロードするコマンド"""
    if ctx.channel.id != CHANNEL:
        return
    
    await ctx.send(f"🔄 Instagramコンテンツをダウンロード中: {url}")
    await download_instagram_post(url, ctx.channel)

# --------------------------------------------------
# 12. エントリーポイント
# --------------------------------------------------
if __name__ == "__main__":
    bot.run(TOKEN)