# discord_video_dl_improved.py
import os, re, asyncio, tempfile, shutil, subprocess, requests, json
from pathlib import Path
from datetime import datetime
import discord
from discord.ext import commands
from urllib.parse import urlparse
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.service_account import Credentials

# --------------------------------------------------
# 1. 環境変数
# --------------------------------------------------
TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_1 = int(os.environ["TARGET_CHANNEL_ID_1"])  # メインチャンネル
CHANNEL_2 = int(os.environ["TARGET_CHANNEL_ID_2"])  # 外注共有用チャンネル
GOOGLE_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")  # Google Driveの保存フォルダID
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")  # サービスアカウントJSON

# --------------------------------------------------
# 2. 外部コマンドと正規表現
# --------------------------------------------------
YTDL = shutil.which("yt-dlp") or "/usr/local/bin/yt-dlp"

# 対応プラットフォームのURL正規表現
PLATFORM_PATTERNS = {
    'twitter': re.compile(r"(https?://(?:www\.)?(?:x\.com|twitter\.com)/\w+/status/\d+)", re.I),
    'instagram': re.compile(r"(https?://(?:www\.)?instagram\.com/(?:p|reel)/([^/?]+))", re.I),
    'tiktok': re.compile(r"(https?://(?:www\.)?tiktok\.com/@[\w.-]+/video/\d+)", re.I),
    'youtube': re.compile(r"(https?://(?:www\.)?(?:youtube\.com/shorts/|youtu\.be/)[\w-]+)", re.I),
}

# 全プラットフォーム統合正規表現
ALL_PLATFORMS_RE = re.compile(
    r"(https?://(?:www\.)?(?:instagram\.com/(?:p|reel)/|x\.com/\w+/status/|twitter\.com/\w+/status/|tiktok\.com/@[\w.-]+/video/|youtube\.com/shorts/|youtu\.be/)[\w-]+)",
    re.I
)

# 画像URLを判定する正規表現
IMAGE_RE = re.compile(
    r"(https?://\S+\.(?:jpg|jpeg|png|gif|webp)(?:\?\S*)?$)", 
    re.I
)

# --------------------------------------------------
# 3. Google Drive 設定
# --------------------------------------------------
def setup_google_drive():
    """Google Drive APIクライアントを設定"""
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        print("Google Drive サービスアカウントJSONが設定されていません")
        return None
    
    try:
        # サービスアカウント情報をJSONから読み込み
        service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        credentials = Credentials.from_service_account_info(
            service_account_info,
            scopes=['https://www.googleapis.com/auth/drive']
        )
        service = build('drive', 'v3', credentials=credentials)
        print("Google Drive API初期化完了")
        return service
    except Exception as e:
        print(f"Google Drive API初期化エラー: {e}")
        return None

# Google Drive サービス初期化
drive_service = setup_google_drive()

# --------------------------------------------------
# 4. Cookie ファイルパス
# --------------------------------------------------
COOKIE_DIR = "/app/cookies"
Path(COOKIE_DIR).mkdir(exist_ok=True)

COOKIE_PATHS = {
    "instagram": Path(COOKIE_DIR) / "instagram_cookies.txt",
    "twitter": Path(COOKIE_DIR) / "twitter_cookies.txt",
    "tiktok": Path(COOKIE_DIR) / "tiktok_cookies.txt",
    "youtube": Path(COOKIE_DIR) / "youtube_cookies.txt",
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

def detect_platform(url: str) -> str:
    """URLからプラットフォームを検出"""
    for platform, pattern in PLATFORM_PATTERNS.items():
        if pattern.search(url):
            return platform
    return "unknown"

# --------------------------------------------------
# 5. Google Drive アップロード関数
# --------------------------------------------------
async def upload_to_drive(file_path: str, filename: str, platform: str) -> tuple[str, str]:
    """
    ファイルをGoogle Driveにアップロードして共有リンクを返す
    Returns: (file_id, shareable_link)
    """
    if not drive_service:
        raise Exception("Google Drive APIが初期化されていません")
    
    try:
        # ファイル名にプラットフォームと日時を追加
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        drive_filename = f"[{platform.upper()}]_{timestamp}_{filename}"
        
        # ファイルメタデータ
        file_metadata = {
            'name': drive_filename,
            'parents': [GOOGLE_DRIVE_FOLDER_ID] if GOOGLE_DRIVE_FOLDER_ID else []
        }
        
        # ファイルをアップロード
        media = MediaFileUpload(file_path, resumable=True)
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()
        
        file_id = file.get('id')
        
        # ファイルを誰でもアクセス可能に設定
        drive_service.permissions().create(
            fileId=file_id,
            body={
                'role': 'reader',
                'type': 'anyone'
            }
        ).execute()
        
        # 共有可能なリンクを生成
        shareable_link = f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"
        
        print(f"Google Driveにアップロード完了: {drive_filename}")
        return file_id, shareable_link
        
    except Exception as e:
        print(f"Google Driveアップロードエラー: {e}")
        raise

# --------------------------------------------------
# 6. Discord Bot 初期化
# --------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 監視対象チャンネルのリスト
MONITORED_CHANNELS = [CHANNEL_1, CHANNEL_2]

# --------------------------------------------------
# 7. メッセージ受信ハンドラ
# --------------------------------------------------
@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot or msg.channel.id not in MONITORED_CHANNELS:
        return
    
    # メッセージ内のすべてのURLを取得
    all_urls = re.findall(r'(https?://\S+)', msg.content)
    
    if all_urls:
        print(f"Found URLs: {all_urls}")
        
        for url in all_urls:
            platform = detect_platform(url)
            print(f"Platform detected: {platform} for URL: {url}")
            
            # 対応プラットフォームの場合はメディアダウンロード
            if platform != "unknown":
                asyncio.create_task(download_and_upload_media(url, msg.channel, platform))
            # 画像URLの場合は画像ダウンロード
            elif is_image_url(url):
                asyncio.create_task(download_and_upload_image(url, msg.channel))
    
    await bot.process_commands(msg)

def is_image_url(url: str) -> bool:
    """URLが画像URLかどうかを判定する"""
    if IMAGE_RE.match(url):
        return True
    if ('pbs.twimg.com' in url) and ('media' in url):
        return True
    return False

# --------------------------------------------------
# 8. 画像ダウンロード＆アップロード関数
# --------------------------------------------------
async def download_and_upload_image(url: str, channel):
    """URLから画像をダウンロードし、Google Driveにアップロードして共有リンクを送信"""
    print(f"▶ START IMAGE DOWNLOAD & UPLOAD: {url}")
    
    tmpdir = tempfile.mkdtemp()
    
    try:
        # URLからファイル名を取得
        parsed_url = urlparse(url)
        path = parsed_url.path
        filename = os.path.basename(path)
        
        if not filename or '.' not in filename:
            filename = f"image_{int(asyncio.get_event_loop().time())}.jpg"
        
        file_path = os.path.join(tmpdir, filename)
        
        # 画像をダウンロード
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
            'Referer': 'https://www.instagram.com/',
        }
        
        response = requests.get(url, headers=headers, stream=True, timeout=30)
        
        if response.status_code == 200:
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1024):
                    if chunk:
                        f.write(chunk)
            
            file_size = os.path.getsize(file_path)
            file_size_mb = file_size / (1024 * 1024)
            print(f"Image downloaded: {filename} ({file_size_mb:.2f} MB)")
            
            # Google Driveにアップロード
            if drive_service:
                try:
                    file_id, shareable_link = await upload_to_drive(file_path, filename, "image")
                    
                    embed = discord.Embed(
                        title="✅ 画像ダウンロード＆アップロード完了",
                        description=f"**元URL:** {url}\n**ファイルサイズ:** {file_size_mb:.2f} MB",
                        color=0x00ff00
                    )
                    embed.add_field(name="Google Drive リンク", value=f"[ファイルを開く]({shareable_link})", inline=False)
                    embed.add_field(name="ダウンロード", value=f"[直接ダウンロード](https://drive.google.com/uc?id={file_id})", inline=False)
                    
                    await channel.send(embed=embed)
                    print(f"✔ Image uploaded to Google Drive: {filename}")
                    
                except Exception as e:
                    # Google Driveアップロードに失敗した場合、Discordに直接送信を試行
                    discord_limit = 8 * 1024 * 1024
                    if file_size <= discord_limit:
                        discord_file = discord.File(file_path)
                        await channel.send(f"⚠️ Google Driveアップロード失敗。Discordに直接送信: {url}", file=discord_file)
                    else:
                        await channel.send(f"❌ ファイルサイズが大きすぎます ({file_size_mb:.2f}MB): {url}")
            else:
                # Google Drive未設定の場合はDiscordに直接送信
                discord_limit = 8 * 1024 * 1024
                if file_size <= discord_limit:
                    discord_file = discord.File(file_path)
                    await channel.send(f"✅ 画像ダウンロード完了: {url}", file=discord_file)
                else:
                    await channel.send(f"⚠️ ファイルサイズが大きすぎます ({file_size_mb:.2f}MB): {url}")
        else:
            await channel.send(f"❌ 画像ダウンロード失敗: {url} (ステータスコード: {response.status_code})")
    
    except Exception as e:
        await channel.send(f"❌ 画像処理中にエラーが発生しました: {url}")
        print(f"✖ IMAGE DOWNLOAD ERROR: {url} - {str(e)}")
    
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# --------------------------------------------------
# 9. メディアダウンロード＆アップロード関数
# --------------------------------------------------
async def download_and_upload_media(url: str, channel, platform: str):
    """URLから動画をダウンロードし、Google Driveにアップロードして共有リンクを送信"""
    print(f"▶ START MEDIA DOWNLOAD & UPLOAD: {url} (Platform: {platform})")
    tmpdir = tempfile.mkdtemp()
    
    try:
        out_tpl = os.path.join(tmpdir, "%(uploader)s_%(id)s.%(ext)s")

        # プラットフォーム別のyt-dlpオプション
        cmd = [YTDL]
        
        if platform == "instagram":
            cmd.extend([
                "-f", "best[ext=mp4]/best",
                "--merge-output-format", "mp4",
                "--write-thumbnail",
                "-o", out_tpl,
                url,
            ])
        elif platform == "tiktok":
            cmd.extend([
                "-f", "best[ext=mp4]/best",
                "--merge-output-format", "mp4",
                "-o", out_tpl,
                url,
            ])
        elif platform == "youtube":
            cmd.extend([
                "-f", "best[height<=1080][ext=mp4]/best[ext=mp4]/best",
                "--merge-output-format", "mp4",
                "-o", out_tpl,
                url,
            ])
        else:  # Twitter/X
            cmd.extend([
                "-S", "vcodec:h264,acodec:m4a,ext:mp4",
                "--merge-output-format", "mp4",
                "-o", out_tpl,
                url,
            ])

        # Cookieファイルがあれば追加
        ck = cookie_for(url)
        if ck and ck.is_file():
            cmd.extend(["--cookies", str(ck)])
            print(f"Using cookie file: {ck}")

        print(f"Running command: {' '.join(cmd)}")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode == 0:
            # ダウンロード成功 - ファイルを検索
            media_files = []
            for ext in ['*.mp4', '*.mov', '*.avi', '*.mkv']:
                media_files.extend(list(Path(tmpdir).glob(ext)))
            
            if not media_files:
                # 動画がない場合は画像を検索
                for ext in ['*.jpg', '*.jpeg', '*.png', '*.webp']:
                    media_files.extend(list(Path(tmpdir).glob(ext)))
            
            if media_files:
                for media_file in media_files[:1]:  # 最初のファイルのみ処理
                    file_size = media_file.stat().st_size
                    file_size_mb = file_size / (1024 * 1024)
                    print(f"Media file found: {media_file.name} ({file_size_mb:.2f} MB)")
                    
                    # Google Driveにアップロード
                    if drive_service:
                        try:
                            file_id, shareable_link = await upload_to_drive(
                                str(media_file), media_file.name, platform
                            )
                            
                            # 埋め込みメッセージを作成
                            embed = discord.Embed(
                                title=f"✅ {platform.upper()} メディアダウンロード＆アップロード完了",
                                description=f"**元URL:** {url}\n**ファイルサイズ:** {file_size_mb:.2f} MB",
                                color=0x00ff00
                            )
                            embed.add_field(
                                name="Google Drive リンク", 
                                value=f"[ファイルを開く]({shareable_link})", 
                                inline=False
                            )
                            embed.add_field(
                                name="ダウンロード", 
                                value=f"[直接ダウンロード](https://drive.google.com/uc?id={file_id})", 
                                inline=False
                            )
                            embed.set_footer(text=f"プラットフォーム: {platform.upper()}")
                            
                            await channel.send(embed=embed)
                            print(f"✔ Media uploaded to Google Drive: {media_file.name}")
                            
                        except Exception as e:
                            print(f"Google Drive upload error: {e}")
                            # フォールバック: Discordに直接送信を試行
                            discord_limit = 8 * 1024 * 1024
                            if file_size <= discord_limit:
                                discord_file = discord.File(str(media_file))
                                await channel.send(
                                    f"⚠️ Google Driveアップロード失敗。Discordに直接送信: {url}", 
                                    file=discord_file
                                )
                            else:
                                await channel.send(
                                    f"❌ ファイルサイズが大きすぎます ({file_size_mb:.2f}MB): {url}\n"
                                    f"Google Driveアップロードも失敗しました。"
                                )
                    else:
                        # Google Drive未設定の場合
                        discord_limit = 8 * 1024 * 1024
                        if file_size <= discord_limit:
                            discord_file = discord.File(str(media_file))
                            await channel.send(f"✅ {platform.upper()} ダウンロード完了: {url}", file=discord_file)
                        else:
                            await channel.send(
                                f"⚠️ ファイルサイズが大きすぎます ({file_size_mb:.2f}MB): {url}\n"
                                f"Google Driveを設定してください。"
                            )
                    break
            else:
                await channel.send(f"❌ ダウンロードしたファイルが見つかりません: {url}")
                print(f"No media files found in {tmpdir}")
        else:
            error_msg = stderr.decode() if stderr else "Unknown error"
            await channel.send(f"❌ {platform.upper()} ダウンロード失敗: {url}")
            print(f"✖ DOWNLOAD FAILED: {url} (rc={proc.returncode}) - {error_msg}")

    except Exception as e:
        await channel.send(f"❌ {platform.upper()} 処理中にエラーが発生しました: {url}")
        print(f"✖ MEDIA DOWNLOAD ERROR: {url} - {str(e)}")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# --------------------------------------------------
# 10. 手動ダウンロードコマンド
# --------------------------------------------------
@bot.command(name="download")
async def manual_download(ctx, url: str, platform: str = None):
    """手動でメディアをダウンロードするコマンド"""
    if ctx.channel.id not in MONITORED_CHANNELS:
        return
    
    if platform is None:
        platform = detect_platform(url)
    
    if platform == "unknown":
        await ctx.send(f"❌ 対応していないプラットフォームです: {url}")
        return
    
    await ctx.send(f"🔄 {platform.upper()} メディアをダウンロード中: {url}")
    await download_and_upload_media(url, ctx.channel, platform)

@bot.command(name="image")
async def image_download_command(ctx, url: str):
    """画像を明示的にダウンロードするコマンド"""
    if ctx.channel.id not in MONITORED_CHANNELS:
        return
    
    await ctx.send(f"🔄 画像のダウンロード中: {url}")
    await download_and_upload_image(url, ctx.channel)

# --------------------------------------------------
# 11. 圧縮ダウンロードコマンド
# --------------------------------------------------
@bot.command(name="compress")
async def compress_download(ctx, url: str):
    """画質を下げて動画をダウンロードするコマンド"""
    if ctx.channel.id not in MONITORED_CHANNELS:
        return
    
    await ctx.send(f"🔄 圧縮モードで処理中: {url}")
    
    tmpdir = tempfile.mkdtemp()
    try:
        platform = detect_platform(url)
        out_tpl = os.path.join(tmpdir, "%(uploader)s_%(id)s.%(ext)s")
        
        # 低画質でダウンロード
        cmd = [
            YTDL,
            "-f", "worst[height<=480][ext=mp4]/worst[ext=mp4]/worst",
            "--merge-output-format", "mp4",
            "-o", out_tpl,
            url,
        ]

        ck = cookie_for(url)
        if ck and ck.is_file():
            cmd.extend(["--cookies", str(ck)])
        
        proc = await asyncio.create_subprocess_exec(*cmd)
        await proc.wait()
        
        if proc.returncode == 0:
            mp4_files = list(Path(tmpdir).glob("*.mp4"))
            
            if mp4_files:
                media_file = mp4_files[0]
                file_size = media_file.stat().st_size
                file_size_mb = file_size / (1024 * 1024)
                
                if drive_service:
                    try:
                        file_id, shareable_link = await upload_to_drive(
                            str(media_file), f"compressed_{media_file.name}", platform
                        )
                        
                        embed = discord.Embed(
                            title="✅ 圧縮ダウンロード＆アップロード完了",
                            description=f"**元URL:** {url}\n**ファイルサイズ:** {file_size_mb:.2f} MB",
                            color=0x00ff00
                        )
                        embed.add_field(name="Google Drive リンク", value=f"[ファイルを開く]({shareable_link})", inline=False)
                        embed.add_field(name="ダウンロード", value=f"[直接ダウンロード](https://drive.google.com/uc?id={file_id})", inline=False)
                        
                        await ctx.send(embed=embed)
                    except Exception:
                        discord_limit = 8 * 1024 * 1024
                        if file_size <= discord_limit:
                            discord_file = discord.File(str(media_file))
                            await ctx.send(f"✅ 圧縮ダウンロード完了 ({file_size_mb:.2f}MB): {url}", file=discord_file)
                        else:
                            await ctx.send(f"❌ 圧縮してもファイルサイズが大きすぎます ({file_size_mb:.2f}MB)")
                else:
                    discord_limit = 8 * 1024 * 1024
                    if file_size <= discord_limit:
                        discord_file = discord.File(str(media_file))
                        await ctx.send(f"✅ 圧縮ダウンロード完了 ({file_size_mb:.2f}MB): {url}", file=discord_file)
                    else:
                        await ctx.send(f"❌ 圧縮してもファイルサイズが大きすぎます ({file_size_mb:.2f}MB)")
            else:
                await ctx.send(f"❌ 圧縮ダウンロードに失敗しました: {url}")
        else:
            await ctx.send(f"❌ 圧縮ダウンロードに失敗しました: {url}")
    
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# --------------------------------------------------
# 12. ヘルプコマンド
# --------------------------------------------------
@bot.command(name="help_dl")
async def help_command(ctx):
    """ダウンロードボットのヘルプを表示"""
    if ctx.channel.id not in MONITORED_CHANNELS:
        return
    
    embed = discord.Embed(
        title="📥 SNS メディアダウンローダー",
        description="対応プラットフォーム: **Twitter/X**, **Instagram**, **TikTok**, **YouTube Shorts**",
        color=0x0099ff
    )
    
    embed.add_field(
        name="🔄 自動ダウンロード",
        value="チャンネルにリンクを貼るだけで自動的にダウンロード・Google Driveアップロード",
        inline=False
    )
    
    embed.add_field(
        name="📋 手動コマンド",
        value="`!download <URL> [platform]` - 手動ダウンロード\n"
              "`!image <URL>` - 画像ダウンロード\n"
              "`!compress <URL>` - 圧縮ダウンロード",
        inline=False
    )
    
    embed.add_field(
        name="☁️ Google Drive",
        value="ダウンロードしたファイルは自動的にGoogle Driveにアップロードされ、共有リンクが生成されます",
        inline=False
    )
    
    await ctx.send(embed=embed)

# --------------------------------------------------
# 13. Bot起動時の処理
# --------------------------------------------------
@bot.event
async def on_ready():
    print(f'{bot.user} としてログインしました')
    print(f'監視チャンネル: {MONITORED_CHANNELS}')
    print(f'Google Drive設定: {"有効" if drive_service else "無効"}')
    
    # チャンネル存在確認
    for channel_id in MONITORED_CHANNELS:
        channel = bot.get_channel(channel_id)
        if channel:
            print(f'チャンネル確認OK: {channel.name} (ID: {channel_id})')
        else:
            print(f'⚠️ チャンネルが見つかりません: {channel_id}')

# --------------------------------------------------
# 14. エントリーポイント
# --------------------------------------------------
if __name__ == "__main__":
    if not TOKEN:
        print("❌ DISCORD_TOKEN環境変数が設定されていません")
        exit(1)
    
    try:
        bot.run(TOKEN)
    except Exception as e:
        print(f"❌ Bot起動エラー: {e}")
        exit(1)