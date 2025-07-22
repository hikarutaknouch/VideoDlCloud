# discord_video_dl.py (完全修正版)
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
CHANNEL_1 = int(os.environ["TARGET_CHANNEL_ID_1"])
CHANNEL_2 = int(os.environ["TARGET_CHANNEL_ID_2"])
GOOGLE_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

DISCORD_SIZE_LIMIT = 8 * 1024 * 1024  # 8MB
YTDL = shutil.which("yt-dlp") or "/usr/local/bin/yt-dlp"

# --------------------------------------------------
# 2. 外部コマンドと正規表現
# --------------------------------------------------
# 対応プラットフォームのURL正規表現
PLATFORM_PATTERNS = {
    'twitter': re.compile(r"(https?://(?:www\.)?(?:x\.com|twitter\.com)/\w+/status/\d+)", re.I),
    'instagram': re.compile(r"(https?://(?:www\.)?instagram\.com/(?:p|reel)/([^/?]+))", re.I),
    'tiktok': re.compile(r"(https?://(?:www\.)?tiktok\.com/@[\w.-]+/video/\d+)", re.I),
    'youtube': re.compile(r"(https?://(?:www\.)?(?:youtube\.com/shorts/|youtu\.be/)[\w-]+)", re.I),
}

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
async def upload_to_drive(file_path: str, filename: str, platform: str):
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
        
        # ファイルメタデータ（共有ドライブ対応）
        file_metadata = {
            'name': drive_filename,
        }
        
        # 共有ドライブかどうかを判定
        if GOOGLE_DRIVE_FOLDER_ID.startswith('0'):
            file_metadata['parents'] = [GOOGLE_DRIVE_FOLDER_ID]
        else:
            file_metadata['parents'] = [GOOGLE_DRIVE_FOLDER_ID] if GOOGLE_DRIVE_FOLDER_ID else []
        
        # ファイルをアップロード
        media = MediaFileUpload(file_path, resumable=True)
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id',
            supportsAllDrives=True
        ).execute()
        
        file_id = file.get('id')
        
        # ファイルを誰でもアクセス可能に設定
        drive_service.permissions().create(
            fileId=file_id,
            body={
                'role': 'reader',
                'type': 'anyone'
            },
            supportsAllDrives=True
        ).execute()
        
        # 共有可能なリンクを生成
        shareable_link = f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"
        
        print(f"Google Driveにアップロード完了: {drive_filename}")
        return file_id, shareable_link
        
    except Exception as e:
        print(f"Google Driveアップロードエラー: {e}")
        raise

# --------------------------------------------------
# 6. ハイブリッド送信関数
# --------------------------------------------------
async def send_hybrid_result(channel, file_path: str, filename: str, platform: str, url: str, file_size: int):
    """
    ファイルサイズに応じてDiscord直接送信 + Google Drive保存を行う
    """
    file_size_mb = file_size / (1024 * 1024)
    
    # 結果格納用
    discord_file_sent = False
    drive_url = None
    file_id = None
    
    try:
        # 1. Google Driveに必ずアップロード
        if drive_service:
            try:
                file_id, drive_url = await upload_to_drive(file_path, filename, platform)
                print(f"✅ Google Drive アップロード成功: {filename}")
            except Exception as e:
                print(f"⚠️ Google Drive アップロード失敗: {e}")
        
        # 2. Discord送信可能サイズかチェック
        discord_file = None
        if file_size <= DISCORD_SIZE_LIMIT:
            try:
                discord_file = discord.File(file_path, filename=filename)
                print(f"✅ Discordファイル準備完了: {filename} ({file_size_mb:.2f}MB)")
                discord_file_sent = True
            except Exception as e:
                print(f"⚠️ Discordファイル準備失敗: {e}")
                discord_file_sent = False
        
        # 3. 結果に応じたメッセージ作成
        if discord_file_sent and drive_url:
            # 両方成功：最高のユーザー体験
            embed = discord.Embed(
                title=f"✅ {platform.upper()} ダウンロード完了",
                description=f"**元URL:** {url}\n**ファイルサイズ:** {file_size_mb:.2f} MB",
                color=0x00ff00
            )
            embed.add_field(
                name="📱 Discordで直接再生", 
                value="⬇️ 添付ファイルをご確認ください", 
                inline=False
            )
            embed.add_field(
                name="☁️ Google Drive保存", 
                value=f"[ファイルを開く]({drive_url})", 
                inline=False
            )
            embed.add_field(
                name="🔗 直接ダウンロード", 
                value=f"[ダウンロード](https://drive.google.com/uc?id={file_id})", 
                inline=False
            )
            embed.set_footer(text=f"プラットフォーム: {platform.upper()} | 両方の保存場所で利用可能")
            
            await channel.send(embed=embed, file=discord_file)
            
        elif discord_file_sent and not drive_url:
            # Discord送信のみ成功
            embed = discord.Embed(
                title=f"✅ {platform.upper()} ダウンロード完了",
                description=f"**元URL:** {url}\n**ファイルサイズ:** {file_size_mb:.2f} MB",
                color=0x00ff00
            )
            embed.add_field(
                name="📱 Discordで直接再生", 
                value="⬇️ 添付ファイルをご確認ください", 
                inline=False
            )
            embed.add_field(
                name="⚠️ Google Drive", 
                value="アップロードに失敗しました", 
                inline=False
            )
            embed.set_footer(text=f"プラットフォーム: {platform.upper()}")
            
            await channel.send(embed=embed, file=discord_file)
            
        elif not discord_file_sent and drive_url:
            # Google Driveのみ成功（ファイルサイズが大きい場合）
            embed = discord.Embed(
                title=f"✅ {platform.upper()} ダウンロード完了",
                description=f"**元URL:** {url}\n**ファイルサイズ:** {file_size_mb:.2f} MB",
                color=0x00ff00
            )
            embed.add_field(
                name="📁 ファイルサイズについて", 
                value=f"ファイルサイズが{DISCORD_SIZE_LIMIT/(1024*1024):.0f}MBを超えているため、Google Driveに保存しました", 
                inline=False
            )
            embed.add_field(
                name="☁️ Google Drive保存", 
                value=f"[ファイルを開く]({drive_url})", 
                inline=False
            )
            embed.add_field(
                name="🔗 直接ダウンロード", 
                value=f"[ダウンロード](https://drive.google.com/uc?id={file_id})", 
                inline=False
            )
            embed.set_footer(text=f"プラットフォーム: {platform.upper()} | Google Driveでアクセス")
            
            await channel.send(embed=embed)
            
        else:
            # 両方失敗
            embed = discord.Embed(
                title=f"❌ {platform.upper()} 保存エラー",
                description=f"**元URL:** {url}\n**ファイルサイズ:** {file_size_mb:.2f} MB",
                color=0xff0000
            )
            embed.add_field(
                name="エラー内容", 
                value="DiscordとGoogle Drive両方への保存に失敗しました", 
                inline=False
            )
            embed.set_footer(text="しばらく時間をおいて再試行してください")
            
            await channel.send(embed=embed)
        
        print(f"✅ ハイブリッド送信完了: Discord={discord_file_sent}, Drive={bool(drive_url)}")
        
    except Exception as e:
        # 全体的なエラーハンドリング
        await channel.send(f"❌ ファイル送信中にエラーが発生しました: {url}\nエラー: {str(e)}")
        print(f"✖ HYBRID SEND ERROR: {url} - {str(e)}")

# --------------------------------------------------
# 7. Discord Bot 初期化
# --------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 監視対象チャンネルのリスト
MONITORED_CHANNELS = [CHANNEL_1, CHANNEL_2]

# --------------------------------------------------
# 8. メッセージ受信ハンドラ
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
                asyncio.create_task(download_and_hybrid_upload(url, msg.channel, platform))
            # 画像URLの場合は画像ダウンロード
            elif is_image_url(url):
                asyncio.create_task(download_and_hybrid_upload_image(url, msg.channel))
    
    await bot.process_commands(msg)

def is_image_url(url: str) -> bool:
    """URLが画像URLかどうかを判定する"""
    if IMAGE_RE.match(url):
        return True
    if ('pbs.twimg.com' in url) and ('media' in url):
        return True
    return False

# --------------------------------------------------
# 9. 画像ダウンロード関数
# --------------------------------------------------
async def download_and_hybrid_upload_image(url: str, channel):
    """URLから画像をダウンロードし、ハイブリッド送信"""
    print(f"▶ START IMAGE HYBRID DOWNLOAD: {url}")
    
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
            print(f"Image downloaded: {filename} ({file_size / (1024*1024):.2f} MB)")
            
            # ハイブリッド送信
            await send_hybrid_result(channel, file_path, filename, "image", url, file_size)
            
        else:
            await channel.send(f"❌ 画像ダウンロード失敗: {url} (ステータスコード: {response.status_code})")
    
    except Exception as e:
        await channel.send(f"❌ 画像処理中にエラーが発生しました: {url}")
        print(f"✖ IMAGE DOWNLOAD ERROR: {url} - {str(e)}")
    
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# --------------------------------------------------
# 10. メディアダウンロード関数
# --------------------------------------------------
async def download_and_hybrid_upload(url: str, channel, platform: str):
    """URLから動画をダウンロードし、ハイブリッド送信"""
    print(f"▶ START MEDIA HYBRID DOWNLOAD: {url} (Platform: {platform})")
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
                    
                    # ハイブリッド送信
                    await send_hybrid_result(channel, str(media_file), media_file.name, platform, url, file_size)
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
# 11. 基本コマンド
# --------------------------------------------------
@bot.command(name="status")
async def bot_status(ctx):
    """Bot の状態を表示"""
    if ctx.channel.id not in MONITORED_CHANNELS:
        return
    
    embed = discord.Embed(
        title="🤖 Bot ステータス",
        color=0x0099ff
    )
    embed.add_field(name="Google Drive", value="✅ 有効" if drive_service else "❌ 無効", inline=True)
    embed.add_field(name="ハイブリッド送信", value="✅ 有効", inline=True)
    embed.add_field(name="ファイルサイズ制限", value=f"{DISCORD_SIZE_LIMIT/(1024*1024):.0f}MB", inline=True)
    
    embed.add_field(
        name="対応プラットフォーム", 
        value="Twitter/X, Instagram, TikTok, YouTube Shorts", 
        inline=False
    )
    
    await ctx.send(embed=embed)

@bot.command(name="help_dl")
async def help_command(ctx):
    """ヘルプを表示"""
    if ctx.channel.id not in MONITORED_CHANNELS:
        return
    
    embed = discord.Embed(
        title="📥 SNS メディアダウンローダー",
        description="対応プラットフォーム: **Twitter/X**, **Instagram**, **TikTok**, **YouTube Shorts**",
        color=0x0099ff
    )
    
    embed.add_field(
        name="🔄 自動ダウンロード",
        value="チャンネルにリンクを貼るだけで自動的にダウンロード",
        inline=False
    )
    
    embed.add_field(
        name="💾 保存先",
        value="• Discord添付（8MB以下）\n• Google Drive（全ファイル）",
        inline=False
    )
    
    embed.add_field(
        name="📋 コマンド",
        value="`!status` - Bot状態確認\n`!help_dl` - このヘルプ",
        inline=False
    )
    
    await ctx.send(embed=embed)

# --------------------------------------------------
# 12. Bot起動時の処理
# --------------------------------------------------
@bot.event
async def on_ready():
    print(f'{bot.user} としてログインしました')
    print(f'監視チャンネル: {MONITORED_CHANNELS}')
    print(f'Google Drive設定: {"有効" if drive_service else "無効"}')
    print(f'Discordファイルサイズ制限: {DISCORD_SIZE_LIMIT/(1024*1024):.0f}MB')
    
    # チャンネル存在確認
    for channel_id in MONITORED_CHANNELS:
        channel = bot.get_channel(channel_id)
        if channel:
            print(f'チャンネル確認OK: {channel.name} (ID: {channel_id})')
        else:
            print(f'⚠️ チャンネルが見つかりません: {channel_id}')

# --------------------------------------------------
# 13. エントリーポイント
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