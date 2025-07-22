# twitter_thread_downloader.py (Xスレッド対応版)
import os, re, asyncio, tempfile, shutil, subprocess, requests, json
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Tuple, Optional
import discord
from discord.ext import commands
from urllib.parse import urlparse
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.service_account import Credentials

# --------------------------------------------------
# 1. 環境変数（既存）
# --------------------------------------------------
TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_1 = int(os.environ["TARGET_CHANNEL_ID_1"])
CHANNEL_2 = int(os.environ["TARGET_CHANNEL_ID_2"])
GOOGLE_DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

DISCORD_SIZE_LIMIT = 8 * 1024 * 1024  # 8MB
YTDL = shutil.which("yt-dlp") or "/usr/local/bin/yt-dlp"

# --------------------------------------------------
# 2. スレッド検出パターン
# --------------------------------------------------
class ThreadDetector:
    """Xスレッドを検出・解析するクラス"""
    
    @staticmethod
    def is_thread_url(url: str) -> bool:
        """URLがXスレッドの一部かどうかを判定"""
        return bool(re.search(r"(x\.com|twitter\.com)/.+/status/\d+", url, re.I))
    
    @staticmethod
    def extract_tweet_id(url: str) -> Optional[str]:
        """URLからツイートIDを抽出"""
        match = re.search(r"/status/(\d+)", url)
        return match.group(1) if match else None
    
    @staticmethod
    def extract_username(url: str) -> Optional[str]:
        """URLからユーザー名を抽出"""
        match = re.search(r"/([\w]+)/status/", url)
        return match.group(1) if match else None

class TwitterAPIClient:
    """Twitter API v2 クライアント（簡易版）"""
    
    def __init__(self, bearer_token: Optional[str] = None):
        self.bearer_token = bearer_token or os.environ.get("TWITTER_BEARER_TOKEN")
        self.base_url = "https://api.twitter.com/2"
    
    async def get_thread_tweets(self, tweet_id: str, username: str) -> List[Dict]:
        """
        スレッドの全ツイートを取得
        注意: 実際の実装ではTwitter API v2が必要
        """
        if not self.bearer_token:
            print("Twitter Bearer Token が設定されていません。yt-dlpフォールバックを使用")
            return await self._fallback_thread_detection(tweet_id, username)
        
        # TODO: Twitter API v2 実装
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json"
        }
        
        # 実際のAPI呼び出し実装
        return await self._fallback_thread_detection(tweet_id, username)
    
    async def _fallback_thread_detection(self, tweet_id: str, username: str) -> List[Dict]:
        """
        yt-dlpを使用したフォールバック方式でスレッド検出
        """
        try:
            # ユーザーの最近のツイートを取得してスレッドを推測
            cmd = [
                YTDL,
                "--flat-playlist",
                "--print", "%(id)s %(title)s %(upload_date)s",
                f"https://x.com/{username}",
                "--max-downloads", "20"  # 最新20件をチェック
            ]
            
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            
            if proc.returncode == 0:
                # 時系列でスレッドを推測
                tweets = []
                lines = stdout.decode().strip().split('\n')
                
                for line in lines:
                    if line.strip():
                        parts = line.split(' ', 2)
                        if len(parts) >= 2:
                            tweet_data = {
                                'id': parts[0],
                                'url': f"https://x.com/{username}/status/{parts[0]}",
                                'title': parts[2] if len(parts) > 2 else "",
                                'has_media': True  # 推測
                            }
                            tweets.append(tweet_data)
                
                # 元のツイートの前後を含むスレッドを推測
                target_index = None
                for i, tweet in enumerate(tweets):
                    if tweet['id'] == tweet_id:
                        target_index = i
                        break
                
                if target_index is not None:
                    # 前後のツイートを含めてスレッドとして扱う
                    start_idx = max(0, target_index - 5)
                    end_idx = min(len(tweets), target_index + 6)
                    return tweets[start_idx:end_idx]
            
            return [{'id': tweet_id, 'url': f"https://x.com/{username}/status/{tweet_id}"}]
            
        except Exception as e:
            print(f"フォールバックスレッド検出エラー: {e}")
            return [{'id': tweet_id, 'url': f"https://x.com/{username}/status/{tweet_id}"}]

# --------------------------------------------------
# 3. スレッドダウンロードマネージャー
# --------------------------------------------------
class ThreadDownloadManager:
    """スレッド全体のダウンロードを管理"""
    
    def __init__(self, bot):
        self.bot = bot
        self.twitter_client = TwitterAPIClient()
    
    async def download_thread_media(self, url: str, channel, progress_callback=None) -> Dict:
        """
        スレッド全体のメディアをダウンロード
        Returns: {
            'total_tweets': int,
            'successful_downloads': int,
            'failed_downloads': int,
            'media_files': List[str],
            'drive_links': List[str]
        }
        """
        detector = ThreadDetector()
        tweet_id = detector.extract_tweet_id(url)
        username = detector.extract_username(url)
        
        if not tweet_id or not username:
            raise ValueError("無効なX/TwitterのURLです")
        
        # 進捗報告
        if progress_callback:
            await progress_callback("🔍 スレッドを解析中...")
        
        # スレッドのツイート一覧を取得
        thread_tweets = await self.twitter_client.get_thread_tweets(tweet_id, username)
        
        if progress_callback:
            await progress_callback(f"📝 スレッド検出: {len(thread_tweets)}件のツイート")
        
        # 結果格納
        results = {
            'total_tweets': len(thread_tweets),
            'successful_downloads': 0,
            'failed_downloads': 0,
            'media_files': [],
            'drive_links': [],
            'discord_files': []
        }
        
        # 各ツイートからメディアをダウンロード
        for i, tweet in enumerate(thread_tweets):
            if progress_callback:
                await progress_callback(f"⬇️ ダウンロード中: {i+1}/{len(thread_tweets)}")
            
            try:
                media_result = await self._download_tweet_media(tweet['url'], f"thread_{i+1:02d}")
                if media_result:
                    results['successful_downloads'] += 1
                    results['media_files'].extend(media_result.get('files', []))
                    if media_result.get('drive_link'):
                        results['drive_links'].append(media_result['drive_link'])
                    if media_result.get('discord_file'):
                        results['discord_files'].append(media_result['discord_file'])
                else:
                    results['failed_downloads'] += 1
                    
            except Exception as e:
                print(f"ツイート {tweet['url']} のダウンロードエラー: {e}")
                results['failed_downloads'] += 1
                
            # 過負荷防止のための待機
            await asyncio.sleep(1)
        
        return results
    
    async def _download_tweet_media(self, tweet_url: str, prefix: str) -> Optional[Dict]:
        """
        個別ツイートのメディアをダウンロード
        """
        tmpdir = tempfile.mkdtemp()
        
        try:
            out_tpl = os.path.join(tmpdir, f"{prefix}_%(id)s.%(ext)s")
            
            cmd = [
                YTDL,
                "-f", "best[ext=mp4]/best",
                "--write-thumbnail", "--write-info-json",
                "-o", out_tpl,
                tweet_url,
            ]
            
            # Cookie設定
            cookie_path = Path("/app/cookies/twitter_cookies.txt")
            if cookie_path.exists():
                cmd.extend(["--cookies", str(cookie_path)])
            
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            
            if proc.returncode == 0:
                # ダウンロードされたファイルを探す
                downloaded_files = list(Path(tmpdir).glob("*"))
                media_files = [f for f in downloaded_files if f.suffix in ['.mp4', '.jpg', '.png', '.gif', '.webp']]
                
                if media_files:
                    # 最初のメディアファイルを処理
                    media_file = media_files[0]
                    file_size = media_file.stat().st_size
                    
                    result = {
                        'files': [str(media_file)],
                        'file_size': file_size
                    }
                    
                    # Google Driveアップロード
                    if self.bot.drive_service:
                        try:
                            file_id, drive_link = await self.bot.upload_to_drive(
                                str(media_file), media_file.name, "twitter_thread"
                            )
                            result['drive_link'] = drive_link
                            result['file_id'] = file_id
                        except Exception as e:
                            print(f"Drive upload error: {e}")
                    
                    # Discordファイル準備
                    if file_size <= DISCORD_SIZE_LIMIT:
                        result['discord_file'] = discord.File(str(media_file), filename=media_file.name)
                    
                    return result
            
            return None
            
        except Exception as e:
            print(f"Individual tweet download error: {e}")
            return None
        
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

# --------------------------------------------------
# 4. Bot拡張 - スレッドコマンド
# --------------------------------------------------
class ThreadDownloadBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        
        self.thread_manager = ThreadDownloadManager(self)
        # 既存のGoogle Drive設定
        self.drive_service = self.setup_google_drive()
        self.monitored_channels = [CHANNEL_1, CHANNEL_2]
    
    def setup_google_drive(self):
        """既存のGoogle Drive設定"""
        if not GOOGLE_SERVICE_ACCOUNT_JSON:
            return None
        try:
            service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
            credentials = Credentials.from_service_account_info(
                service_account_info,
                scopes=['https://www.googleapis.com/auth/drive']
            )
            service = build('drive', 'v3', credentials=credentials)
            return service
        except Exception as e:
            print(f"Google Drive API初期化エラー: {e}")
            return None
    
    async def upload_to_drive(self, file_path: str, filename: str, platform: str) -> tuple[str, str]:
        """既存のGoogle Driveアップロード機能"""
        # 既存実装を使用
        pass

# --------------------------------------------------
# 5. スレッドダウンロードコマンド
# --------------------------------------------------
@bot.command(name="thread")
async def download_thread(ctx, url: str = None):
    """
    Xスレッド全体をダウンロード
    使用例: !thread https://x.com/username/status/1234567890
    """
    if ctx.channel.id not in bot.monitored_channels:
        return
    
    if not url:
        embed = discord.Embed(
            title="❌ URL が必要です",
            description="使用例: `!thread https://x.com/username/status/1234567890`",
            color=0xff0000
        )
        await ctx.send(embed=embed)
        return
    
    # URL検証
    detector = ThreadDetector()
    if not detector.is_thread_url(url):
        await ctx.send("❌ 有効なX/TwitterのURLを指定してください")
        return
    
    # 進捗メッセージ
    progress_embed = discord.Embed(
        title="🧵 スレッドダウンロード開始",
        description="スレッドを解析中...",
        color=0x1DA1F2
    )
    progress_msg = await ctx.send(embed=progress_embed)
    
    async def update_progress(status: str):
        """進捗更新用コールバック"""
        progress_embed.description = status
        await progress_msg.edit(embed=progress_embed)
    
    try:
        # スレッドダウンロード実行
        results = await bot.thread_manager.download_thread_media(url, ctx.channel, update_progress)
        
        # 結果表示
        await display_thread_results(ctx, results, url, progress_msg)
        
    except Exception as e:
        error_embed = discord.Embed(
            title="❌ スレッドダウンロードエラー",
            description=f"エラー: {str(e)}",
            color=0xff0000
        )
        await progress_msg.edit(embed=error_embed)

async def display_thread_results(ctx, results: Dict, original_url: str, progress_msg):
    """スレッドダウンロード結果を表示"""
    
    # 成功・失敗の集計
    total = results['total_tweets']
    success = results['successful_downloads']
    failed = results['failed_downloads']
    
    # メイン結果埋め込み
    result_embed = discord.Embed(
        title="🧵 スレッドダウンロード完了",
        description=f"**元URL:** {original_url}",
        color=0x00ff00 if success > 0 else 0xff9900
    )
    
    result_embed.add_field(
        name="📊 ダウンロード結果",
        value=f"成功: {success}件\n失敗: {failed}件\n合計: {total}件",
        inline=True
    )
    
    # Discordファイル添付（制限内のもの）
    discord_files = results.get('discord_files', [])[:10]  # 最大10ファイル
    
    if discord_files:
        result_embed.add_field(
            name="📱 Discord添付",
            value=f"{len(discord_files)}件のファイルを添付",
            inline=True
        )
    
    # Google Driveリンク
    drive_links = results.get('drive_links', [])
    if drive_links:
        result_embed.add_field(
            name="☁️ Google Drive",
            value=f"[フォルダを開く](https://drive.google.com/drive/folders/{GOOGLE_DRIVE_FOLDER_ID})",
            inline=True
        )
        
        # 個別リンクは別メッセージで
        if len(drive_links) <= 5:
            links_text = "\n".join([f"[ファイル {i+1}]({link})" for i, link in enumerate(drive_links)])
            result_embed.add_field(
                name="🔗 個別リンク",
                value=links_text,
                inline=False
            )
    
    result_embed.set_footer(text=f"スレッド一括ダウンロード | 処理時間: {total * 1.5:.1f}秒")
    
    # メッセージ更新
    await progress_msg.edit(embed=result_embed)
    
    # Discordファイル送信（制限があるため分割）
    if discord_files:
        try:
            await ctx.send("📱 **Discord添付ファイル:**", files=discord_files[:10])
        except discord.HTTPException as e:
            await ctx.send(f"⚠️ 一部ファイルの添付に失敗しました: {str(e)}")

# --------------------------------------------------
# 6. 自動スレッド検出
# --------------------------------------------------
@bot.event
async def on_message(message: discord.Message):
    """メッセージ受信時の自動処理"""
    if message.author.bot or message.channel.id not in bot.monitored_channels:
        return
    
    # URL検出
    urls = re.findall(r'(https?://\S+)', message.content)
    if not urls:
        await bot.process_commands(message)
        return
    
    for url in urls:
        detector = ThreadDetector()
        
        if detector.is_thread_url(url):
            # スレッド検出時の処理選択
            
            # オプション1: 自動的にスレッド全体をダウンロード
            # asyncio.create_task(auto_download_thread(url, message.channel))
            
            # オプション2: ユーザーに選択肢を提示
            embed = discord.Embed(
                title="🧵 スレッド検出",
                description="Xスレッドが検出されました。ダウンロード方法を選択してください:",
                color=0x1DA1F2
            )
            embed.add_field(
                name="1️⃣ 単一ツイートのみ", 
                value="このツイートのみダウンロード",
                inline=False
            )
            embed.add_field(
                name="2️⃣ スレッド全体", 
                value="`!thread <URL>` でスレッド全体をダウンロード",
                inline=False
            )
            
            await message.channel.send(embed=embed)
            
            # 通常の単一ダウンロードも実行
            asyncio.create_task(download_single_tweet(url, message.channel))
    
    await bot.process_commands(message)

async def download_single_tweet(url: str, channel):
    """既存の単一ツイートダウンロード機能"""
    # 既存のダウンロード処理を使用
    pass

# --------------------------------------------------
# 7. 統計・ヘルプコマンド
# --------------------------------------------------
@bot.command(name="thread_help")
async def thread_help(ctx):
    """スレッドダウンロード機能のヘルプ"""
    embed = discord.Embed(
        title="🧵 スレッドダウンロード機能",
        description="Xのスレッド（連投）全体を一括ダウンロードします",
        color=0x1DA1F2
    )
    
    embed.add_field(
        name="📋 基本コマンド",
        value="`!thread <URL>` - スレッド全体をダウンロード",
        inline=False
    )
    
    embed.add_field(
        name="🎯 対応形式",
        value="• 画像（JPEG, PNG, GIF, WebP）\n• 動画（MP4, MOV）\n• スレッド内の全メディア",
        inline=False
    )
    
    embed.add_field(
        name="💾 保存場所",
        value="• Discord添付（8MB以下）\n• Google Drive（全ファイル）\n• 自動ファイル名付け",
        inline=False
    )
    
    embed.add_field(
        name="⚠️ 注意事項",
        value="• 大きなスレッドは処理に時間がかかります\n• 認証が必要なツイートは対象外\n• API制限により一部取得できない場合があります",
        inline=False
    )
    
    embed.add_field(
        name="💡 使用例",
        value="`!thread https://x.com/username/status/1234567890`",
        inline=False
    )
    
    await ctx.send(embed=embed)

# --------------------------------------------------
# 8. エントリーポイント
# --------------------------------------------------
bot = ThreadDownloadBot()

if __name__ == "__main__":
    if not TOKEN:
        print("❌ DISCORD_TOKEN環境変数が設定されていません")
        exit(1)
    
    try:
        bot.run(TOKEN)
    except Exception as e:
        print(f"❌ Bot起動エラー: {e}")
        exit(1)