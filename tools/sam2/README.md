# SAM2 點選去背服務

參考圖去背用。跑在**自己的 venv**，不與 ComfyUI 共用環境。

## 為什麼獨立

- 它跟 ComfyUI 搶同一張卡。不用時要能把模型整個從 VRAM 卸掉，同程序做不到乾淨釋放。
- ComfyUI 的環境是整套流程的核心，不值得為了省 2GB 磁碟去冒 pip 動到它 numpy/opencv 的風險。

## 安裝

```
python -m venv E:/h3-sam2/.venv
E:/h3-sam2/.venv/Scripts/python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126
E:/h3-sam2/.venv/Scripts/python -m pip install ultralytics
copy sam2_service.py E:/h3-sam2/
```

權重 `sam2_b.pt`（約 160MB）首次推論時自動下載到 `E:/h3-sam2/models/`。
位置可用環境變數 `SAM2_DIR` 改，伺服器端預設找 `E:/h3-sam2`。

## 行為

- 由 h3-server 按需啟動，不必手動開
- 閒置 `SAM2_IDLE_UNLOAD`（預設 180 秒）卸掉模型並清空 CUDA 快取
- 閒置 `SAM2_IDLE_EXIT`（預設 900 秒）整支結束，下次再被拉起來
- 只聽 127.0.0.1

## 端點

- `GET /health` → `{ok, loaded, port}`
- `GET /unload` → 立刻卸模型
- `POST /segment` → `{image: dataURL, points: [[x,y]…], labels: [1|0…], bg: "#rrggbb"|null}`
  回 `{png: dataURL, w, h, covered, elapsed}`。`bg` 留空＝透明去背。

## 一個非踩不可的坑

ultralytics 的 `predict.py` 對扁平的 `(N,2)` 會做

```python
points, labels = points[:, None, :], labels[:, None]
```

把 N 個點當成「N 個各自獨立的單點提示」，產生 N 個互不相干的遮罩。負向點於是只會變成自己那一張遮罩，聯集之後**完全沒有作用**。必須包成 `(1, N, 2)` 才是「同一物件、N 個提示」，`ndim==3` 就不會被重塑。

實測同一張圖同一個綠點：扁平形狀加紅點覆蓋率 48.41% → 48.41%（無效）；
包成巢狀後 48.41% → 40.55%，排除 156,515 像素。
