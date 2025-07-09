FROM python:3.12-slim

# ---- system deps ----
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg curl ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

# yt-dlpを確実にインストール
RUN pip install --no-cache-dir yt-dlp

# ログをバッファリングさせずに標準出力へ
ENV PYTHONUNBUFFERED=1

# Cookieファイル用ディレクトリ作成
RUN mkdir -p /app/cookies

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY discord_video_dl_improved.py .

CMD ["python", "discord_video_dl_improved.py"]