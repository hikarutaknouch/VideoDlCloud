FROM python:3.12-slim

# --- 依存パッケージ ---
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg  curl ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

# ▶↑ `ca-certificates`  … https で yt-dlp が証明書エラーになるのを防ぐ  
# ▶↑ `tzdata`           … 日本時間のログがずれないように（サイズ +2 MB 程度）

ENV PYTHONUNBUFFERED=1        # ログがバッファされずリアルタイムに流れる

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY discord_video_dl.py .

CMD ["python", "discord_video_dl.py"]
