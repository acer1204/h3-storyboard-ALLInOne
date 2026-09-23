# 去背服務

參考圖去背用。跑在**自己的 venv**，不與 ComfyUI 共用環境。

前端用的是 **BiRefNet**（自動去背，不用點選）。服務裡也還留著 **SAM2** 的點選分割，
前端目前沒有接，但端點還在，要做「兩個角色選其一」時可以直接用。

## 為什麼獨立

- 它跟 ComfyUI 搶同一張卡，而 ComfyUI 的環境是整套流程的核心，不值得為了省 2GB 磁碟去冒 pip 動到它 numpy/opencv 的風險。
- h3-server 本身是純標準函式庫，沒有 torch。併進去等於讓一支整天開著的程序背上 torch 與 CUDA。
- 走 ComfyUI 的話去背會排在影片生成後面。那是互動操作，按下去要馬上有反應，不能等算圖跑完。

## 安裝

```
python -m venv E:/h3-matte/.venv
E:/h3-matte/.venv/Scripts/python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126
E:/h3-matte/.venv/Scripts/python -m pip install ultralytics safetensors
copy matte_service.py E:/h3-matte/
copy birefnet.py     E:/h3-matte/
```

位置可用環境變數 `CUT_DIR` 改，伺服器端預設找 `E:/h3-matte`。

### Docker

repo 根目錄的 `docker-compose.yml` 會用這個資料夾的 `Dockerfile` 建一個 `h3-matte` 容器
（基底 `pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime`，GPU 由 compose 保留）。
h3-server 容器帶著 `CUT_URL=http://h3-matte:9996`，看到這個就**不會** subprocess 拉本機 venv，
只等容器回應。容器裡的差別都是環境變數：

| 變數 | 容器裡 | 為什麼 |
|---|---|---|
| `MATTE_BIND` | `0.0.0.0` | 預設只聽 127.0.0.1，另一個容器連不到 |
| `MATTE_IDLE_EXIT` | `0` | 生命週期歸 docker 管，閒置不自己結束（模型照樣三分鐘卸掉） |
| `BREF_MODELS` | `/models/background_removal` | 掛的是 repo 的 `models/` |
| `SAM_MODELS_DIR` | `/models/sam` | SAM2 權重下載到這裡，重建容器不用再下載 |

### 權重

| 用途 | 檔案 | 放哪 |
|---|---|---|
| BiRefNet 自動去背 | `birefnet.safetensors`（424MB）、`lucida.safetensors`（844MB） | repo 的 `models/background_removal/`；沒有的話找 ComfyUI 的 |
| SAM2 點選分割 | `sam2_b.pt`（154MB） | `SAM_MODELS_DIR`，第一次用時在背景自動下載 |

ComfyUI 在同一台時，去背權重直接沿用它那一份。ComfyUI 搬到別台之後只能自己放一份，
放在 repo 的 `models/background_removal/`（不進 git）；h3-server 啟動去背服務時會把同一個位置
用 `BREF_MODELS` 傳過去，兩邊才不會各自猜出不同的資料夾。

SAM2 權重是從 GitHub releases 下載的，實測這條線只有 50-100 KB/s，要二十幾分鐘。
以前是在第一次推論時同步下載，下載期間整支服務的鎖一直握著，連 BiRefNet 都跟著卡死。
現在改成背景下載：還沒下載完時 `/segment` 立刻回 503 並附上目前的 MB 數。
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

卸模型是同程序做的（`del` + `empty_cache()`），不需要結束程序。實測載入權重占 654MB、跑過一次去背後占到 2.3GB，`unload` 都收得回來。程序結束只多收回 **230MB** 的 CUDA context——那是 PyTorch 一碰 GPU 就拿走、程序活著就不還的那一塊。

所以「為了釋放 VRAM 才要分開」並不成立。分開的理由是上面那兩個。

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
