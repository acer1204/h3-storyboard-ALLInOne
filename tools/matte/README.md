# 去背服務

參考圖去背用。跑在**自己的 venv**，不與 ComfyUI 共用環境。

前端用的是 **BiRefNet**（自動去背，不用點選）。服務裡也還留著 **SAM2** 的點選分割，
前端目前沒有接，但端點還在，要做「兩個角色選其一」時可以直接用。

## 為什麼獨立

- 它跟 ComfyUI 搶同一張卡。不用時要能把模型整個從 VRAM 卸掉，同程序做不到乾淨釋放。
- 走 ComfyUI 的話去背會排在影片生成後面。那是互動操作，按下去要馬上有反應，不能等算圖跑完。
- ComfyUI 的環境是整套流程的核心，不值得為了省 2GB 磁碟去冒 pip 動到它 numpy/opencv 的風險。

## 安裝

```
python -m venv E:/h3-matte/.venv
E:/h3-matte/.venv/Scripts/python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126
E:/h3-matte/.venv/Scripts/python -m pip install ultralytics safetensors
copy matte_service.py E:/h3-matte/
copy birefnet.py     E:/h3-matte/
```

位置可用環境變數 `CUT_DIR` 改，伺服器端預設找 `E:/h3-matte`。

### 權重

| 用途 | 檔案 | 放哪 |
|---|---|---|
| BiRefNet 自動去背 | `birefnet.safetensors`（424MB）、`lucida.safetensors`（844MB） | ComfyUI 的 `models/background_removal/` |
| SAM2 點選分割 | `sam2_b.pt`（約 160MB） | `E:/h3-matte/models/`，首次推論自動下載 |

去背權重直接沿用 ComfyUI 那一份，不另外複製。位置可用 `BREF_MODELS` 改。
`Comfy-Org/BiRefNet` 與 `Comfy-Org/sam3.1` 都在 HuggingFace 上。

## 端點

| | |
|---|---|
| `GET /health` | 活著沒、哪個去背模型在 VRAM 裡 |
| `GET /matte/models` | 可用的去背模型與各自的 image_size |
| `GET /matte/warm?model=` | 背景預先載入，立刻回應不等它 |
| `POST /matte` | `{image, model, bg}` → 去背 PNG（連續 alpha） |
| `POST /segment` | SAM2 點選分割，`{image, points, labels, bbox, refine}` |
| `GET /unload` | 手動放掉 VRAM |

## 生命週期

按需啟動。閒置 **3 分鐘**卸掉模型（釋放 VRAM），**90 分鐘**整支結束。

程序活得比模型久是刻意的：`import torch` 要兩秒、建模型結構再一秒多，檔案快取冷的時候
會膨脹到十幾秒。模型卸掉就不佔 VRAM 了，但程序留著可以省掉重跑那些 import。

## 移植 BiRefNet 時踩到的

`birefnet.py` 是從 ComfyUI 的 `comfy/background_removal/birefnet.py` 搬過來的，改了三處：

1. `operations.*` 只用到 `Conv2d / Linear / BatchNorm2d / LayerNorm`，comfy 那邊就是 nn 的薄包裝，換成 `torch.nn` 行為一致。
2. `cast_to_input` 與 `CastBiasWeightContext` 是為了 comfy 的卸載機制做 dtype/device 轉換，這裡不用那套，換成 `.to()`。
3. `optimized_attention` 帶著 `skip_reshape=True` 兩個旗標呼叫，等價於標準的 `scaled_dot_product_attention`。

還有一個非改不可的：`img_mask = torch.zeros(...)` 原本沒指定 dtype。comfy 的 manual_cast
會把權重追著輸入轉，所以那邊沒事；直接用 nn 時沒人代勞，float32 的 mask 加到 fp16 的
attention 上會把整條推回 fp32，然後在 `attn @ v` 炸掉。

搬完與 ComfyUI 比對過：平均絕對差 0.00258，二值化後不一致 0.0055%，差異只是 fp16 累加順序。

## SAM2 的坑（點選分割那條路才會遇到）

ultralytics 的 `predict.py` 對扁平的 `(N,2)` 會做 `points[:, None, :]`，把 N 個點當成
「N 個各自獨立的單點提示」，產生 N 張互不相干的遮罩。那樣負向點只會變成自己那一張，
聯集起來等於完全沒作用。包成 `(1, N, 2)` 才是「同一個物件、N 個提示」。
