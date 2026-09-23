# H3 Storyboard 後端。h3-server.py 只用標準函式庫，唯一的外部依賴是 ffmpeg
# （影片審查抽幀、長片合併、封面縮圖）。
#
# 程式碼不烤進映像：compose 把整個 repo 掛到 /app，歷史、上傳、設定都留在
# 原本那個資料夾。所以改 h3-webui.html 按 F5 就生效，改 h3-server.py 要
# `docker compose restart h3-server`——跟用 start_app.bat 時的習慣一樣。
FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata \
 && rm -rf /var/lib/apt/lists/*

# 時區要跟主機一致：歷史紀錄、任務清單、長片檔名都用本地時間，
# 容器預設的 UTC 會讓同一批紀錄差八小時。
# PYTHONDONTWRITEBYTECODE：/app 是掛進來的 repo，別往裡面丟 __pycache__。
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Taipei

WORKDIR /app
EXPOSE 9998
CMD ["python", "h3-server.py"]
