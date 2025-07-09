# 環境変数設定ガイド

## 必須環境変数

### Discord設定
```bash
DISCORD_TOKEN=your_discord_bot_token_here
TARGET_CHANNEL_ID_1=123456789012345678  # メインチャンネルのID
TARGET_CHANNEL_ID_2=876543210987654321  # 外注共有用チャンネルのID
```

### Google Drive設定（推奨）
```bash
GOOGLE_DRIVE_FOLDER_ID=1a2b3c4d5e6f7g8h9i0j  # 保存先フォルダのID
GOOGLE_SERVICE_ACCOUNT_JSON='{"type": "service_account", "project_id": "your-project", ...}'  # サービスアカウントJSONの内容
```

## Google Drive API設定手順

### 1. Google Cloud Console設定
1. [Google Cloud Console](https://console.cloud.google.com/) にアクセス
2. 新しいプロジェクトを作成
3. Google Drive APIを有効にする
4. サービスアカウントを作成
5. サービスアカウントキー（JSON）をダウンロード

### 2. Google Drive フォルダ設定
1. Google Driveでボット用フォルダを作成
2. フォルダのIDをURLから取得（`https://drive.google.com/drive/folders/FOLDER_ID_HERE`）
3. サービスアカウントのメールアドレスにフォルダの編集権限を付与

### 3. 環境変数設定
- `GOOGLE_DRIVE_FOLDER_ID`: 作成したフォルダのID
- `GOOGLE_SERVICE_ACCOUNT_JSON`: ダウンロードしたJSONファイルの内容をそのまま文字列として設定

## Fly.io デプロイ時の設定

```bash
# Discord設定
flyctl secrets set DISCORD_TOKEN=your_discord_bot_token_here
flyctl secrets set TARGET_CHANNEL_ID_1=123456789012345678
flyctl secrets set TARGET_CHANNEL_ID_2=876543210987654321

# Google Drive設定
flyctl secrets set GOOGLE_DRIVE_FOLDER_ID=1a2b3c4d5e6f7g8h9i0j
flyctl secrets set GOOGLE_SERVICE_ACCOUNT_JSON='{"type": "service_account", "project_id": "your-project", ...}'
```

## チャンネルIDの取得方法

1. Discordで開発者モードを有効にする
2. 対象チャンネルを右クリック
3. 「IDをコピー」を選択

## 対応プラットフォーム

- **Twitter/X**: `https://x.com/user/status/123456789`
- **Instagram**: `https://instagram.com/p/ABC123` または `https://instagram.com/reel/ABC123`
- **TikTok**: `https://tiktok.com/@user/video/123456789`
- **YouTube Shorts**: `https://youtube.com/shorts/ABC123` または `https://youtu.be/ABC123`

## 使用方法

### 自動ダウンロード
- 監視チャンネルに対応プラットフォームのリンクを貼るだけ
- 自動的にダウンロード・Google Driveアップロード・共有リンク生成

### 手動コマンド
- `!download <URL>` - 手動ダウンロード
- `!image <URL>` - 画像ダウンロード
- `!compress <URL>` - 圧縮ダウンロード
- `!help_dl` - ヘルプ表示

## 機能

✅ **複数チャンネル対応**: 2つのチャンネルで同時監視  
✅ **クラウドストレージ**: Google Driveに自動アップロード  
✅ **共有リンク生成**: 外注への共有が簡単  
✅ **マルチプラットフォーム**: Twitter/X, Instagram, TikTok, YouTube Shorts対応  
✅ **ファイルサイズ制限解決**: 大きなファイルもGoogle Drive経由で共有  
✅ **エラーハンドリング**: 失敗時のフォールバック機能