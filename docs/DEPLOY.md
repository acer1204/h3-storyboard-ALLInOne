# 移機與三台機器的分工

有兩種跑法，資料是同一份：

- **Docker（建議）**：`docker compose up -d --build`，網頁伺服器與去背服務各一個容器。
- **Windows 直接跑**：雙擊 `start_app.bat`，去背服務由 h3-server 按需拉起。

兩種都讀 repo 資料夾裡的 `config.json`、`history/`、`uploads/`……所以可以隨時換著用。

## git 帶不走的東西

`.gitignore` 排除了所有本機狀態。clone 之後這些要自己處理：

| 路徑 | 是什麼 | 怎麼辦 |
|---|---|---|
| `config.json` | 服務位址與埠 | **一定要建**，格式見下 |
| `models/background_removal/` | BiRefNet 去背權重（兩個共 1.3GB） | 從 HuggingFace `Comfy-Org/BiRefNet` 下載，或從舊機器複製 |
| `models/sam/` | SAM2 點選權重（154MB） | 可以不管，第一次用時自己下載（但很慢，見下） |
| `settings.json` | 跨瀏覽器共用的設定 | 不建就是全部預設，可以不管 |
| `drafts.json` | 生成清單與格子的草稿 | 想接續工作才複製 |
| `history/`、`uploads/`、`thumbs/` | 歷史紀錄與圖片 | 想保留舊紀錄才複製 |
| `lessons/`、`prompts/`、`movies/` | 經驗庫、自建 prompt 組、長片專案 | 同上 |
| `queue/` | 任務清單 | 同上；裡面還在跑的任務，搬過來後會自己跟 ComfyUI 對帳 |
| `output/` | 遠端 ComfyUI 成品的本機鏡像 | 不用複製，需要時會自己拉回來 |
| `venv/` | Python 環境 | **不要複製**，`start_app.bat` 會自己重建（Docker 用不到） |
| `TEST IMAGE/` | 測試素材 | 要測才複製 |

不用 Docker 時，去背服務在 `E:/h3-matte`，**不在這個 repo 裡**，要另外搬（含它的 `.venv`）。
Docker 則是直接用 repo 裡的 `tools/matte/` 建映像，不需要那個資料夾。

## config.json

```json
{
  "llama_url": "https://llm.example.com",
  "comfy_url": "https://comfy.example.com:10010",
  "media_root": "",
  "workflow_dir": "",
  "workflow_current": "205646_00001_audio_V2.json",
  "gpu_free_mb": 4000,
  "bind": "0.0.0.0",
  "port": 9998
}
```

- **空字串＝用當下環境的預設。** `workflow_dir` 空著就是 repo 的 `workflows/`，`media_root`
  空著就是 repo 的 `output/`。Windows 上是 `F:/.../output`、容器裡是 `/app/output`，
  同一份檔案兩邊都能讀。在網頁上按儲存時，等於預設的值也會寫成空字串。
- ComfyUI **在同一台**時，`media_root` 可以直接指到它的 `output` 目錄（不是 `output/video`）。
- ComfyUI **在別台**時 `media_root` 留空。它的 output 不在這台的磁碟上，成品會在任務完成時
  從 ComfyUI 的 `/view` 拉回 `output/`，審查、長片合併、封面讀的都是這份鏡像。
  媒體庫要看到 ComfyUI 那邊的全部，ComfyUI 啟動時要加 **`--enable-assets`**
  （它會在背景索引 output，只讀檔案資訊，四千個檔幾秒）。沒加的話媒體庫只看得到拉回來過的檔案。
  列出來但還沒拉回來的標「遠端」：點開時才下載，也不能從這裡刪。
- `https://` 的 ComfyUI（反向代理後面）也可以：進度與即時預覽的 websocket 會走 TLS。
  網址帶路徑前綴（`https://host/comfy`）也行。
- `bind: "0.0.0.0"` 才能從區網其他機器連進來。

環境變數可以覆寫：`H3_LLAMA_URL`、`H3_COMFY_URL` 等（`H3_` + 大寫鍵名）。
環境變數給的值**不會**被網頁的儲存寫回 `config.json`。
去背服務另有 `CUT_URL`（外部管理的服務，Docker 用）、`CUT_DIR`、`CUT_PORT`、`BREF_MODELS`。

## Docker

```
docker compose up -d --build        # 第一次，或改了 Dockerfile
docker compose restart h3-server    # 改了 h3-server.py
docker compose restart h3-matte     # 改了 tools/matte/*.py
docker compose logs -f h3-server    # 主控台
```

- 整個 repo 掛進 `h3-server` 的 `/app`。改 `h3-webui.html` 一樣按 F5 就好。
- `h3-matte` 要 NVIDIA GPU。Docker Desktop 的 WSL2 後端就能用，不用另外裝 toolkit。
- 容器時區是 `Asia/Taipei`，跟主機一致；不設的話歷史紀錄的時間會差八小時。
- 網頁在 `http://localhost:9998/`；去背服務的 9996 只開給本機除錯
  （`curl localhost:9996/health`），h3-server 走容器網路連它。

### SAM2 權重下載很慢

`sam2_b.pt` 是 ultralytics 從 GitHub releases 抓的。實測這條線只有 50-100 KB/s，
154MB 要二十幾分鐘。第一次按「點選去除／點選補回」時會開始在背景下載，
那一下會回「權重下載中，目前 x MB」，下載完再點一次就好。下載期間 BiRefNet 去背照常可用。
下載到的檔案在 `models/sam/`，重建容器不用再下載。

## 三台機器的分工

目標是三件事同時跑不互相搶卡：

```
  ┌─ 這台（Docker）──────────┐   ┌─ 主機 A ──────┐   ┌─ 主機 B ──────┐
  │ h3-server  :9998         │   │ llama-server  │   │ ComfyUI       │
  │ h3-matte   :9996    GPU  │   │          GPU  │   │          GPU  │
  │ （網頁、去背）           │   │ （劇情生成）  │   │ （算影片）    │
  └──────────────────────────┘   └───────────────┘   └───────────────┘
```

`config.json` 就照上面填 A 與 B 的位址。設定完之後：

**「與 GPU 共用」請維持「自動（建議）」。** 它會自己判斷：
`llama_url` 與 `comfy_url` 都不在本機時 → **直接放行，不擋**。
所以三台分開的配置下，去背永遠不會被擋——這正是你要的效果。

在容器裡，指向同一台主機的服務要寫成 `host.docker.internal`，它會被當成本機
（同一張卡），「自動」就會照常把關。

`系統設定 → 去背景` 那一行小字會直接告訴你現在會不會被擋。
也可以打 `/api/cutout/status` 看 `comfy_local` / `llama_local` / `blocked`。

### 不要選「一律允許同時使用」

那個選項會**關掉保護**。只有在三者共用同一張卡、而你確定 VRAM 夠時才有意義。
分機的配置下「自動」已經是全放行，選它沒有好處，只會在你哪天把服務搬回同一台時炸掉。

### LLM 在反向代理後面

有些代理只開放 `/v1/*`（`/props`、`/health` 回 401），生成完全不受影響。
「測試連線」會在 `/props` 失敗時改問 `/v1/models`，結果後面會註明是從哪裡讀的。

### 檢查清單

1. `config.json` 的三個位址都填對，A 與 B 的服務先起來
2. `docker compose up -d --build`（或跑 `start_app.bat`，第一次會建 venv）
3. 開 `http://localhost:9998/` → `系統設定 → 連線與生成` → 按「測試連線」
4. `系統設定 → 去背景` 確認「與 GPU 共用」是**自動**，且小字說「目前放行」
5. 隨便去背一張圖。Docker 看 `docker compose logs h3-matte`；
   bat 則是主控台會印 `[cut] 啟動去背服務`
6. 想用 SAM3 的話，ComfyUI 那台要有 `SAM3_Detect` 節點與
   `sam3.1_multiplex_fp16.safetensors`；沒有的話模型下拉選單就不會出現 SAM3
7. 打開任務清單，已完成的卡片要有封面（ComfyUI 在別台時是從它那邊拉回來的）
8. 打開 ComfyUI 媒體庫，說明文字要寫「經由 /api/assets 列出」；
   寫「只列得出已經拉回這台的檔案」就是 ComfyUI 沒加 `--enable-assets`

## 主控台（start_app.bat）

h3-server 的視窗會印 log。**請把「快速編輯模式」關掉**
（右鍵標題列 → 內容 → 選項 → 取消勾選「快速編輯模式」）。

開著的話，只要不小心在視窗裡點一下，輸出就會暫停——以前這會讓**整個 API 停擺**
而靜態檔照常送，看起來像「網頁活著但什麼都不能按」。現在 log 已經改成非阻塞，
不會再卡住服務，但暫停期間的 log 會被丟掉（會印一行說丟了幾行）。

去背服務的輸出在 `h3-matte.log`。Docker 沒有這個問題，log 用 `docker compose logs` 看。

## Claude Desktop

工作資料夾選到這個 repo，它會讀 `CLAUDE.md`——裡面有架構、慣例，
以及一整節「踩過的坑」。那節是這個專案最難重新發現的部分，值得先看。

**`~/.claude/.../memory/MEMORY.md` 是那台機器上的個人記憶，不會跟著 repo 走。**
要轉移的東西請寫進 `CLAUDE.md`，那份才會跟著專案跑。
