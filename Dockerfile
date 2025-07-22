FROM python:3.12-slim

# ---- system deps ----
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg curl ca-certificates tzdata procps \
 && rm -rf /var/lib/apt/lists/*

# yt-dlpを確実にインストール
RUN pip install --no-cache-dir yt-dlp

# ログをバッファリングさせずに標準出力へ
ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Tokyo

# 作業ディレクトリとファイル作成
WORKDIR /app

# ログディレクトリとCookieディレクトリ作成
RUN mkdir -p /app/logs /app/cookies

# 依存関係をインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションファイルをコピー
COPY discord_video_dl.py .

# ログファイルの作成
RUN touch /app/bot.log

# 権限設定
RUN chmod +x /app/discord_video_dl.py

# ヘルスチェック
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import os; exit(0 if os.path.exists('/app/bot.log') else 1)"

CMD ["python", "discord_video_dl.py"]