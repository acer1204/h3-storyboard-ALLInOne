# 去背服務（BiRefNet 自動去背＋SAM2 點選分割），用本機 GPU。
# 這個基底已經帶 torch 2.6 / torchvision 0.21 / numpy，只補剩下的。
FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime

# ultralytics 會拉 opencv-python，它要 libGL；這裡沒有畫面可開，換成 headless 版。
# 兩個套件共用 cv2 這個資料夾，先裝後拆會把它一起拆掉，所以 headless 要強制重裝。
RUN pip install --no-cache-dir ultralytics safetensors \
 && pip uninstall -y opencv-python \
 && pip install --no-cache-dir --force-reinstall --no-deps opencv-python-headless

# MATTE_IDLE_EXIT=0：在容器裡由 docker 管生命週期，閒置不自己結束
# （模型照樣在閒置三分鐘後從 VRAM 卸掉）。
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Taipei \
    MATTE_BIND=0.0.0.0 \
    MATTE_PORT=9996 \
    MATTE_IDLE_EXIT=0 \
    BREF_MODELS=/models/background_removal \
    SAM_MODELS_DIR=/models/sam \
    YOLO_CONFIG_DIR=/tmp/ultralytics

WORKDIR /app
COPY matte_service.py birefnet.py ./
EXPOSE 9996
CMD ["python", "matte_service.py"]
