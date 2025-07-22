# discord_video_dl.py (完全改良版)
import os, re, asyncio, tempfile, shutil, subprocess, requests, json, signal, sys
import logging, psutil
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Tuple
from asyncio import Semaphore
import discord
from discord.ext import commands
from urllib.parse import urlparse
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.service_account import Credentials

# --------------------------------------------------
# 1. ログ設定
# --------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/app/bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# --------------------------------------------------
# 2. 環境変数と設定
# --------------------------------------------------
TOKEN = os.environ.get("DISCORD_TOKEN")
CHANNEL_1 = int(os.environ.get("TARGET_CHANNEL_ID_1", "0"))
CHANNEL_2 = int(os.environ.get("TARGET_CHANNEL_ID_2", "0"))
GOOGLE_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

# 新しい設定オプション
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "100"))
AUTO_DELETE_TEMP = os.environ.get("AUTO_DELETE_TEMP", "true").lower() == "true"
DOWNLOAD_THUMBNAILS = os.environ.get("DOWNLOAD_THUMBNAILS", "false").lower() == "true"
QUALITY_PREFERENCE = os.environ.get("QUALITY_PREFERENCE", "best")  # best, medium, low
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "3"))

DISCORD_SIZE_LIMIT = 8 * 1024 * 1024  # 8MB
YTDL = shutil.which("yt-dlp") or "/usr/local/bin/yt-dlp"

# 同時ダウンロード数制限
download_semaphore = Semaphore(MAX_CONCURRENT_DOWNLOADS)

# --------------------------------------------------
# 3. 統計情報とプラットフォーム設定
# --------------------------------------------------
download_stats = {
    "total_downloads": 0,
    "successful_downloads": 0,
    "failed_downloads": 0,
    "platforms": {"instagram": 0, "tiktok": 0, "youtube": 0, "twitter": 0, "image": 0},
    "start_time": datetime.now()
}

PLATFORM_PATTERNS = {
    'twitter': re.compile(r"(https?://(?:www\.)?(?:x\.com|twitter\.com)/\w+/status/\d+)", re.I),
    'instagram': re.compile(r"(https?://(?:www\.)?instagram\.com/(?:p|reel)/([^/?]+))", re.I),
    'tiktok': re.compile(r"(https?://(?:www\.)?tiktok\.com/@[\w.-]+/video/\d+)", re.I),
    'youtube': re.compile(r"(https?://(?:www\.)?(?:youtube\.com/shorts/|youtu\.be/)[\w-]+)", re.I),
}

IMAGE_RE = re.compile(r"(https?://\S+\.(?:jpg|jpeg|png|gif|webp)(?:\?\S*)?$)", re.I)

QUALITY_FORMATS = {
    "best": "best[height<=1080][ext=mp4]/best[ext=mp4]/best",
    "medium": "best[height<=720][ext=mp4]/best[ext=mp4]/best",
    "low": "best[height<=480][ext=mp4]/best[ext=mp4]/best"
}

PLATFORM_LIMITS = {
    "instagram": {"max_size_mb": 50, "timeout": 60},
    "tiktok": {"max_size_mb": 30, "timeout": 45},
    "youtube": {"max_size_mb": 100, "timeout": 90},
    "twitter": {"max_size_mb": 25, "timeout": 30}
}

# --------------------------------------------------
# 4. Cookie管理クラス
# --------------------------------------------------
class CookieManager:
    def __init__(self):
        self.cookie_dir = Path("/app/cookies")
        self.cookie_dir.mkdir(exist_ok=True)
        
        self.cookie_paths = {
            "instagram": self.cookie_dir / "instagram_cookies.txt",
            "twitter": self.cookie_dir / "twitter_cookies.txt",
            "tiktok": self.cookie_dir / "tiktok_cookies.txt",
            "youtube": self.cookie_dir / "youtube_cookies.txt",
        }
        
        logger.info(f"Cookie管理初期化完了: {self.cookie_dir}")
    
    def is_cookie_valid(self, platform: str) -> bool:
        """Cookieの有効性をチェック"""
        cookie_path = self.cookie_paths.get(platform)
        if not cookie_path or not cookie_path.exists():
            return False
        
        try:
            mtime = datetime.fromtimestamp(cookie_path.stat().st_mtime)
            if datetime.now() - mtime > timedelta(days=30):
                logger.warning(f"{platform} cookieが古い可能性があります")
                return False
            return True
        except Exception as e:
            logger.error(f"Cookie検証エラー ({platform}): {e}")
            return False
    
    def get_cookie_for_url(self, url: str) -> Optional[Path]:
        """URLからCookieファイルのパスを取得"""
        if "instagram.com" in url:
            platform = "instagram"
        elif "twitter.com" in url or "x.com" in url:
            platform = "twitter"
        elif "tiktok.com" in url:
            platform = "tiktok"
        elif "youtube.com" in url or "youtu.be" in url:
            platform = "youtube"
        else:
            return None
        
        cookie_path = self.cookie_paths.get(platform)
        if cookie_path and self.is_cookie_valid(platform):
            return cookie_path
        return None
    
    def get_status(self) -> dict:
        """全プラットフォームのCookie状態を取得"""
        status = {}
        for platform, path in self.cookie_paths.items():
            if path.exists():
                try:
                    mtime = datetime.fromtimestamp(path.stat().st_mtime)
                    age = datetime.now() - mtime
                    status[platform] = {
                        "exists": True,
                        "valid": age.days < 30,
                        "age_days": age.days
                    }
                except Exception:
                    status[platform] = {"exists": True, "valid": False, "age_days": -1}
            else:
                status[platform] = {"exists": False, "valid": False, "age_days": -1}
        return status

cookie_manager = CookieManager()

# --------------------------------------------------
# 5. Google Drive設定
# --------------------------------------------------
def setup_google_drive():
    """Google Drive APIクライアントを設定"""
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        logger.warning("Google Drive サービスアカウントJSONが設定されていません")
        return None
    
    try:
        service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        credentials = Credentials.from_service_account_info(
            service_account_info,
            scopes=['https://www.googleapis.com/auth/drive']
        )
        service = build('drive', 'v3', credentials=credentials)
        logger.info("Google Drive API初期化完了")
        return service
    except Exception as e:
        logger.error(f"Google Drive API初期化エラー: {e}")
        return None

drive_service = setup_google_drive()

# --------------------------------------------------
# 6. ユーティリティ関数
# --------------------------------------------------
def detect_platform(url: str) -> str:
    """URLからプラットフォームを検出"""
    for platform, pattern in PLATFORM_PATTERNS.items():
        if pattern.search(url):
            return platform
    return "unknown"

def is_image_url(url: str) -> bool:
    """URLが画像URLかどうかを判定する"""
    if IMAGE_RE.match(url):
        return True
    if ('pbs.twimg.com' in url) and ('media' in url):
        return True
    return False

def get_format_for_platform(platform: str) -> str:
    """プラットフォーム別の最適なフォーマットを取得"""
    base_format = QUALITY_FORMATS.get(QUALITY_PREFERENCE, QUALITY_FORMATS["best"])
    
    limit = PLATFORM_LIMITS.get(platform, {"max_size_mb": MAX_FILE_SIZE_MB})
    max_size = limit["max_size_mb"]
    
    if platform == "instagram":
        return f"best[ext=mp4][filesize<{max_size}M]/best[ext=mp4]/best"
    elif platform == "tiktok":
        return f"best[ext=mp4][filesize<{max_size}M]/best[ext=mp4]/best"
    elif platform == "youtube":
        return f"best[height<=1080][ext=mp4][filesize<{max_size}M]/best[ext=mp4]/best"
    else:  # Twitter
        return f"best[ext=mp4][filesize<{max_size}M]/best[ext=mp4]/best"

def update_stats(platform: str, success: bool):
    """統計情報を更新"""
    download_stats["total_downloads"] += 1
    if success:
        download_stats["successful_downloads"] += 1
    else:
        download_stats["failed_downloads"] += 1
    
    if platform in download_stats["platforms"]:
        download_stats["platforms"][platform] += 1

# --------------------------------------------------
# 7. Google Drive アップロード（改良版）
# --------------------------------------------------
async def upload_to_drive(file_path: str, filename: str, platform: str) -> Tuple[Optional[str], Optional[str]]:
    """
    ファイルをGoogle Driveにアップロードして共有リンクを返す
    Returns: (file_id, shareable_link) or (None, None) if failed
    """
    if not drive_service:
        logger.warning("Google Drive APIが初期化されていません")
        return None, None
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # ファイルサイズチェック
            file_size = os.path.getsize(file_path)
            if file_size > 500 * 1024 * 1024:  # 500MB制限
                logger.warning(f"ファイルサイズが大きすぎます: {file_size / (1024*1024):.2f}MB")
                return None, None
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            drive_filename = f"[{platform.upper()}]_{timestamp}_{filename}"
            
            file_metadata = {
                'name': drive_filename,
            }
            
            if GOOGLE_DRIVE_FOLDER_ID:
                file_metadata['parents'] = [GOOGLE_DRIVE_FOLDER_ID]
            
            logger.info(f"Google Driveアップロード開始: {drive_filename}")
            
            media = MediaFileUpload(file_path, resumable=True)
            file = drive_service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id',
                supportsAllDrives=True
            ).execute()
            
            file_id = file.get('id')
            
            # 権限設定のリトライ
            try:
                drive_service.permissions().create(
                    fileId=file_id,
                    body={
                        'role': 'reader',
                        'type': 'anyone'
                    },
                    supportsAllDrives=True
                ).execute()
            except Exception as perm_error:
                logger.warning(f"権限設定失敗（リトライ {attempt+1}）: {perm_error}")
                if attempt == max_retries - 1:
                    raise perm_error
                await asyncio.sleep(2)
                continue
            
            shareable_link = f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"
            logger.info(f"Google Driveアップロード成功: {drive_filename}")
            return file_id, shareable_link
            
        except Exception as e:
            logger.error(f"Google Driveアップロードエラー（試行 {attempt+1}/{max_retries}）: {e}")
            if attempt == max_retries - 1:
                return None, None
            await asyncio.sleep(2 ** attempt)  # 指数バックオフ
    
    return None, None

# --------------------------------------------------
# 8. ハイブリッド送信関数（改良版）
# --------------------------------------------------
async def send_hybrid_result(channel, file_path: str, filename: str, platform: str, url: str, file_size: int):
    """
    ファイルサイズに応じてDiscord直接送信 + Google Drive保存を行う
    """
    file_size_mb = file_size / (1024 * 1024)
    logger.info(f"ハイブリッド送信開始: {filename} ({file_size_mb:.2f}MB)")
    
    discord_file_sent = False
    drive_url = None
    file_id = None
    
    try:
        # 1. Google Driveに必ずアップロード
        if drive_service:
            try:
                file_id, drive_url = await upload_to_drive(file_path, filename, platform)
                if drive_url:
                    logger.info(f"✅ Google Drive アップロード成功: {filename}")
                else:
                    logger.warning(f"⚠️ Google Drive アップロード失敗: {filename}")
            except Exception as e:
                logger.error(f"⚠️ Google Drive アップロード例外: {e}")
        
        # 2. Discord送信可能サイズかチェック
        discord_file = None
        if file_size <= DISCORD_SIZE_LIMIT:
            try:
                discord_file = discord.File(file_path, filename=filename)
                logger.info(f"✅ Discordファイル準備完了: {filename}")
                discord_file_sent = True
            except Exception as e:
                logger.error(f"⚠️ Discordファイル準備失敗: {e}")
                discord_file_sent = False
        
        # 3. 結果に応じたメッセージ作成と送信
        embed = discord.Embed(
            title=f"{'✅' if (discord_file_sent or drive_url) else '❌'} {platform.upper()} ダウンロード{'完了' if (discord_file_sent or drive_url) else '失敗'}",
            description=f"**元URL:** {url}\n**ファイルサイズ:** {file_size_mb:.2f} MB",
            color=0x00ff00 if (discord_file_sent or drive_url) else 0xff0000
        )
        
        if discord_file_sent and drive_url:
            # 両方成功：最高のユーザー体験
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
            embed.add_field(
                name="エラー内容", 
                value="DiscordとGoogle Drive両方への保存に失敗しました", 
                inline=False
            )
            embed.set_footer(text="しばらく時間をおいて再試行してください")
            await channel.send(embed=embed)
        
        success = discord_file_sent or bool(drive_url)
        update_stats(platform, success)
        logger.info(f"✅ ハイブリッド送信完了: Discord={discord_file_sent}, Drive={bool(drive_url)}")
        
    except Exception as e:
        await channel.send(f"❌ ファイル送信中にエラーが発生しました: {url}\nエラー: {str(e)}")
        logger.error(f"✖ HYBRID SEND ERROR: {url} - {str(e)}")
        update_stats(platform, False)

# --------------------------------------------------
# 9. 画像ダウンロード関数（改良版）
# --------------------------------------------------
async def download_and_hybrid_upload_image(url: str, channel):
    """URLから画像をダウンロードし、ハイブリッド送信"""
    async with download_semaphore:
        logger.info(f"▶ START IMAGE DOWNLOAD: {url}")
        
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
                logger.info(f"Image downloaded: {filename} ({file_size / (1024*1024):.2f} MB)")
                
                # ハイブリッド送信
                await send_hybrid_result(channel, file_path, filename, "image", url, file_size)
                
            else:
                await channel.send(f"❌ 画像ダウンロード失敗: {url} (ステータスコード: {response.status_code})")
                update_stats("image", False)
        
        except Exception as e:
            await channel.send(f"❌ 画像処理中にエラーが発生しました: {url}")
            logger.error(f"✖ IMAGE DOWNLOAD ERROR: {url} - {str(e)}")
            update_stats("image", False)
        
        finally:
            if AUTO_DELETE_TEMP:
                shutil.rmtree(tmpdir, ignore_errors=True)

# --------------------------------------------------
# 10. メディアダウンロード関数（改良版）
# --------------------------------------------------
async def download_and_hybrid_upload(url: str, channel, platform: str):
    """URLから動画をダウンロードし、ハイブリッド送信"""
    async with download_semaphore:
        logger.info(f"▶ START MEDIA DOWNLOAD: {url} (Platform: {platform})")
        tmpdir = tempfile.mkdtemp()
        
        try:
            out_tpl = os.path.join(tmpdir, "%(uploader)s_%(id)s.%(ext)s")
            
            # プラットフォーム別のyt-dlpオプション
            cmd = [YTDL]
            
            format_str = get_format_for_platform(platform)
            timeout = PLATFORM_LIMITS.get(platform, {"timeout": 60})["timeout"]
            
            if platform == "instagram":
                cmd.extend([
                    "-f", format_str,
                    "--merge-output-format", "mp4",
                    "--socket-timeout", str(timeout),
                    "-o", out_tpl,
                ])
                if DOWNLOAD_THUMBNAILS:
                    cmd.append("--write-thumbnail")
            elif platform == "tiktok":
                cmd.extend([
                    "-f", format_str,
                    "--merge-output-format", "mp4",
                    "--socket-timeout", str(timeout),
                    "-o", out_tpl,
                ])
            elif platform == "youtube":
                cmd.extend([
                    "-f", format_str,
                    "--merge-output-format", "mp4",
                    "--socket-timeout", str(timeout),
                    "-o", out_tpl,
                ])
            else:  # Twitter/X
                cmd.extend([
                    "-S", "vcodec:h264,acodec:m4a,ext:mp4",
                    "--merge-output-format", "mp4",
                    "--socket-timeout", str(timeout),
                    "-o", out_tpl,
                ])
            
            # Cookieファイルがあれば追加
            cookie_path = cookie_manager.get_cookie_for_url(url)
            if cookie_path:
                cmd.extend(["--cookies", str(cookie_path)])
                logger.info(f"Using cookie file: {cookie_path}")
            
            cmd.append(url)
            
            logger.info(f"Running command: {' '.join(cmd[:5])}... (省略)")
            
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), 
                    timeout=timeout + 30
                )
            except asyncio.TimeoutError:
                proc.kill()
                await channel.send(f"❌ {platform.upper()} ダウンロードタイムアウト: {url}")
                logger.error(f"✖ DOWNLOAD TIMEOUT: {url}")
                update_stats(platform, False)
                return
            
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
                        
                        # ファイルサイズチェック
                        if file_size_mb > MAX_FILE_SIZE_MB:
                            await channel.send(f"❌ ファイルサイズが制限を超えています: {file_size_mb:.2f}MB > {MAX_FILE_SIZE_MB}MB")
                            logger.warning(f"File too large: {file_size_mb:.2f}MB")
                            update_stats(platform, False)
                            break
                        
                        logger.info(f"Media file found: {media_file.name} ({file_size_mb:.2f} MB)")
                        
                        # ハイブリッド送信
                        await send_hybrid_result(channel, str(media_file), media_file.name, platform, url, file_size)
                        break
                else:
                    await channel.send(f"❌ ダウンロードしたファイルが見つかりません: {url}")
                    logger.error(f"No media files found in {tmpdir}")
                    update_stats(platform, False)
            else:
                error_msg = stderr.decode() if stderr else "Unknown error"
                await channel.send(f"❌ {platform.upper()} ダウンロード失敗: {url}")
                logger.error(f"✖ DOWNLOAD FAILED: {url} (rc={proc.returncode}) - {error_msg}")
                update_stats(platform, False)
                
        except Exception as e:
            await channel.send(f"❌ {platform.upper()} 処理中にエラーが発生しました: {url}")
            logger.error(f"✖ MEDIA DOWNLOAD ERROR: {url} - {str(e)}")
            update_stats(platform, False)
        
        finally:
            if AUTO_DELETE_TEMP:
                shutil.rmtree(tmpdir, ignore_errors=True)

# --------------------------------------------------
# 11. Discord Bot 初期化
# --------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 監視対象チャンネルのリスト
MONITORED_CHANNELS = [ch for ch in [CHANNEL_1, CHANNEL_2] if ch > 0]

# --------------------------------------------------
# 12. メッセージ受信ハンドラ（改良版）
# --------------------------------------------------
async def process_with_semaphore(func, *args):
    """セマフォを使用して同時実行数を制限"""
    try:
        await func(*args)
    except Exception as e:
        logger.error(f"Processing error: {e}")

@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot or msg.channel.id not in MONITORED_CHANNELS:
        return
    
    # メッセージ内のすべてのURLを取得
    all_urls = re.findall(r'(https?://\S+)', msg.content)
    
    if all_urls:
        logger.info(f"Found URLs: {all_urls}")
        
        # 複数URLの並列処理（制限付き）
        tasks = []
        for url in all_urls:
            platform = detect_platform(url)
            logger.info(f"Platform detected: {platform} for URL: {url}")
            
            if platform != "unknown":
                task = asyncio.create_task(
                    process_with_semaphore(download_and_hybrid_upload, url, msg.channel, platform)
                )
                tasks.append(task)
            elif is_image_url(url):
                task = asyncio.create_task(
                    process_with_semaphore(download_and_hybrid_upload_image, url, msg.channel)
                )
                tasks.append(task)
        
        # すべてのタスクの完了を待つ（エラーを無視）
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    
    await bot.process_commands(msg)

# --------------------------------------------------
# 13. メモリ監視
# --------------------------------------------------
async def monitor_memory():
    """メモリ使用量を監視"""
    while True:
        try:
            process = psutil.Process()
            memory_info = process.memory_info()
            memory_mb = memory_info.rss / 1024 / 1024
            
            if memory_mb > 800:  # 800MB超過で警告
                logger.warning(f"高メモリ使用量: {memory_mb:.2f}MB")
            
            await asyncio.sleep(300)  # 5分ごとにチェック
        except Exception as e:
            logger.error(f"Memory monitoring error: {e}")
            await asyncio.sleep(300)

# --------------------------------------------------
# 14. コマンド（改良版）
# --------------------------------------------------
@bot.command(name="status")
async def bot_status(ctx):
    """Bot の状態を表示"""
    if ctx.channel.id not in MONITORED_CHANNELS:
        return
    
    # システム情報
    process = psutil.Process()
    memory_mb = process.memory_info().rss / 1024 / 1024
    uptime = datetime.now() - download_stats["start_time"]
    
    embed = discord.Embed(
        title="🤖 Bot ステータス",
        color=0x0099ff
    )
    embed.add_field(name="Google Drive", value="✅ 有効" if drive_service else "❌ 無効", inline=True)
    embed.add_field(name="メモリ使用量", value=f"{memory_mb:.1f}MB", inline=True)
    embed.add_field(name="稼働時間", value=f"{uptime.days}日 {uptime.seconds//3600}時間", inline=True)
    
    embed.add_field(name="ダウンロード制限", value=f"{MAX_FILE_SIZE_MB}MB", inline=True)
    embed.add_field(name="同時実行数", value=f"{MAX_CONCURRENT_DOWNLOADS}", inline=True)
    embed.add_field(name="品質設定", value=QUALITY_PREFERENCE, inline=True)
    
    embed.add_field(
        name="対応プラットフォーム", 
        value="Twitter/X, Instagram, TikTok, YouTube Shorts", 
        inline=False
    )
    
    await ctx.send(embed=embed)

@bot.command(name="stats")
async def show_stats(ctx):
    """ダウンロード統計を表示"""
    if ctx.channel.id not in MONITORED_CHANNELS:
        return
    
    success_rate = (download_stats["successful_downloads"] / max(download_stats["total_downloads"], 1)) * 100
    
    embed = discord.Embed(
        title="📊 ダウンロード統計",
        color=0x0099ff
    )
    embed.add_field(name="総ダウンロード数", value=download_stats["total_downloads"], inline=True)
    embed.add_field(name="成功率", value=f"{success_rate:.1f}%", inline=True)
    embed.add_field(name="失敗数", value=download_stats["failed_downloads"], inline=True)
    
    platform_text = "\n".join([
        f"{platform.capitalize()}: {count}"
        for platform, count in download_stats["platforms"].items()
    ])
    embed.add_field(name="プラットフォーム別", value=platform_text, inline=False)
    
    uptime = datetime.now() - download_stats["start_time"]
    embed.set_footer(text=f"稼働開始: {download_stats['start_time'].strftime('%Y-%m-%d %H:%M')} (稼働時間: {uptime.days}日)")
    
    await ctx.send(embed=embed)

@bot.command(name="cookies")
async def check_cookies(ctx):
    """Cookie状態を確認"""
    if ctx.channel.id not in MONITORED_CHANNELS:
        return
    
    embed = discord.Embed(
        title="🍪 Cookie状態確認",
        color=0x0099ff
    )
    
    cookie_status = cookie_manager.get_status()
    for platform, status in cookie_status.items():
        if status["exists"]:
            if status["valid"]:
                value = f"✅ 有効 (更新: {status['age_days']}日前)"
            else:
                value = f"⚠️ 古い (更新: {status['age_days']}日前)"
        else:
            value = "❌ なし"
        
        embed.add_field(
            name=platform.capitalize(),
            value=value,
            inline=True
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
        value="`!status` - Bot状態確認\n`!stats` - ダウンロード統計\n`!cookies` - Cookie状態確認\n`!help_dl` - このヘルプ",
        inline=False
    )
    
    await ctx.send(embed=embed)

# --------------------------------------------------
# 15. Bot起動時・終了時の処理
# --------------------------------------------------
@bot.event
async def on_ready():
    logger.info(f'{bot.user} としてログインしました')
    logger.info(f'監視チャンネル: {MONITORED_CHANNELS}')
    logger.info(f'Google Drive設定: {"有効" if drive_service else "無効"}')
    logger.info(f'設定 - ファイルサイズ制限: {MAX_FILE_SIZE_MB}MB, 品質: {QUALITY_PREFERENCE}, 同時実行: {MAX_CONCURRENT_DOWNLOADS}')
    
    # チャンネル存在確認
    for channel_id in MONITORED_CHANNELS:
        channel = bot.get_channel(channel_id)
        if channel:
            logger.info(f'チャンネル確認OK: {channel.name} (ID: {channel_id})')
        else:
            logger.warning(f'⚠️ チャンネルが見つかりません: {channel_id}')
    
    # メモリ監視タスク開始
    asyncio.create_task(monitor_memory())

@bot.event
async def on_disconnect():
    logger.warning("Botが切断されました")

@bot.event
async def on_resumed():
    logger.info("Bot接続が復旧しました")

# --------------------------------------------------
# 16. グレースフルシャットダウン
# --------------------------------------------------
def signal_handler(sig, frame):
    logger.info('Bot終了処理中...')
    asyncio.create_task(bot.close())
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# --------------------------------------------------
# 17. エントリーポイント
# --------------------------------------------------
if __name__ == "__main__":
    if not TOKEN:
        logger.error("❌ DISCORD_TOKEN環境変数が設定されていません")
        exit(1)
    
    if not MONITORED_CHANNELS:
        logger.error("❌ 監視チャンネルが設定されていません")
        exit(1)
    
    try:
        logger.info("Bot起動中...")
        bot.run(TOKEN)
    except Exception as e:
        logger.error(f"❌ Bot起動エラー: {e}")
        exit(1)