# H3 Storyboard — 給 Claude 的專案說明

這份檔案給任何在這個資料夾工作的 Claude 看。使用者以**繁體中文**溝通，
程式碼註解也一律中文——請沿用，不要改成英文。

---

## 這是什麼

把文字或圖片變成 MiniMax H3（海螺）的影音 Prompt，再送 ComfyUI 算成影片。
生成靠本地／區網的 llama-server（多模態 Qwen），格式靠官方的 h3-prompt-writing skill。

五種模式：T2VA（純文字）、I2VA（首幀）、FL2VA（首尾幀）、L2VA（末幀）、REF2VA（參考圖）。
另有 FL2VA Movie（多段接龍長片）。每個模式有獨立的格子、生成清單與歷史。

## 檔案結構

| 檔案 | 是什麼 |
|---|---|
| `h3-webui.html` | **整個前端，單一檔案**（約 6000 行）。HTML + CSS + JS 全在裡面 |
| `h3-server.py` | 後端，**只用標準函式庫**（沒有 numpy/cv2/PIL/flask），`ThreadingHTTPServer` on :9998 |
| `start_app.bat` | Windows 啟動器。會自己建 venv |
| `docker-compose.yml`、`docker/` | Docker 部署（網頁＋去背兩個容器）。**目前是用這個在跑** |
| `skills/` | 官方與自建的 prompt skill（`h3-prompt-writing` 是核心） |
| `workflows/` | ComfyUI 工作流（含 SAM3 那幾個） |
| `tools/drawing-ocr/` | 另一個專案：工程圖標註定位，已獨立發佈 |
| `tools/matte/` | 去背服務原始碼與它的 Dockerfile |
| `ui2api.py` | ComfyUI「Save」格式的工作流 → 可執行的 API 節點圖 |

**`config.json`、`settings.json`、`drafts.json`、`history/`、`uploads/`、`models/`、`output/` 都不進 git**（見 `.gitignore`）。
換機器要重建——細節看 `docs/DEPLOY.md`。

## 外部服務

| 服務 | 預設位置 | 誰啟動 |
|---|---|---|
| llama-server | `config.json` 的 `llama_url` | 使用者自己（別台，https 反向代理） |
| ComfyUI | `config.json` 的 `comfy_url` | 使用者自己（別台，https 反向代理） |
| 去背服務 h3-matte | Docker：`h3-matte` 容器；bat：`E:/h3-matte`，port 9996 | Docker：compose；bat：**h3-server 按需 subprocess 拉起來** |

去背服務有 BiRefNet（去背）與 SAM2（點選分割）。它有 `numpy/cv2/torch`，h3-server **沒有**——
需要影像運算時要想清楚放哪一層，或乾脆放前端（canvas）。
h3-server 看到環境變數 `CUT_URL` 就把去背服務當成外部管理的，不會自己拉。

真實的主機名稱只寫在 `config.json`。**不要寫進任何會進 git 的檔案**（repo 是公開的）。

---

## 改完要做什麼才生效

| 改了 | 怎麼生效 |
|---|---|
| `h3-webui.html` | **F5 就好**。伺服器每次請求都重讀這個檔 |
| `h3-server.py` | Docker：`docker compose restart h3-server`；bat：重開 bat |
| `tools/matte/*.py` | `docker compose restart h3-matte` |
| `tools/matte/Dockerfile`、`docker/*` | `docker compose up -d --build` |
| `skills/*` | F5（伺服器每次讀目錄） |

使用者說過「停止服務 我自己開bat」——**沒被要求就不要自己啟動或重啟他的服務**
（容器也一樣）。

---

## 踩過的坑（最重要的一節）

這些都是實際量出來的，不是推測。換機器之後最難重新發現的就是這些。

### 1. Windows 主控台的 QuickEdit 會讓整個 API 停擺

在 h3-server 的黑色視窗裡**點一下或選到文字**，輸出就會暫停，任何 `stderr.write` 永遠卡住。
而 `log_message` 對每個 `/api/` 都寫一行 log，於是**所有 API 卡死、靜態檔照常送**——
看起來像「網頁活著但什麼都不能按」。

判斷方式：`/api/comfy/status/xxx`（程式裡唯一不寫 log 的 API）如果秒回、其他全部逾時，就是這個。
現在 log 已經改成非阻塞佇列，但**如果又出現「全部卡住」，先想這件事**。
解法：點那個視窗按 `Esc`。

### 2. 去背的 GPU 閘門可能被使用者關掉

`系統設定 → 去背景 → 與 GPU 共用`。設成「一律允許」（`cutBusy="1"`）時，
`cut_gpu_block()` 第一行就 return，**完全不問 ComfyUI**。
ComfyUI 算圖中去背 → BiRefNet 載到已經滿的卡上 → Windows 把顯存分頁 → 兩邊一起爬。

### 3. `comfy_busy()` 問不到時不能當成「不忙」

它回 `None` 而 `None` 是假值。而且**這台機器上沒開的本機埠是 timeout 不是連線被拒**，
所以不能用錯誤型別分辨「沒在跑」與「忙到不回話」。現在改用「之前聯絡上過嗎」判。

### 4. BiRefNet 是「顯著物件」分割，不是「人」

人坐在床上，床就被算成主體。**拉高 `image_size`（1024/1536/2048）與換權重都救不了**——
實測三張失敗圖的角落殘留是 1.00 / 0.97-1.00 / 0.98-1.00，幾乎沒差。那是語意判斷。

有效的是 **SAM3 的 text prompt 閘**：`alpha = bref_alpha × dilate(person_mask)`。
實測床整片消失、角色完好；三張本來就乾淨的主體保留 0.997-0.999、誤刪 0.07-0.18%。

- prompt 的 `:N` 是**注意力權重不是人數**，而且會飽和（`:99` = `:2`）。
  但兩個人時只寫 `person` 會**漏掉一個**（覆蓋 27.6% vs `person:2` 的 65.2%），所以預設是 `person:2`。
- `SAM3_Detect` 的 `individual_masks` 必須是 `False`，否則每個人各一張遮罩，
  只取到其中一張會把另一個人當背景刪掉。
- 那個工作流有**兩個輸出節點**：`PreviewImage`（RGB，沒 alpha）與 `SaveImage`（RGBA）。
  要挑有 alpha 的那張，否則 alpha 全 255 會被當成「全都是主體」。

### 5. 瀏覽器對單一 origin 只有約 6 條連線

存草稿會上傳全解析度 base64。以前沒有防重入，好幾輪疊起來就把連線佔滿，
去背的 POST 只能排隊——而它的逾時計時器**在排隊時就已經在走**。
所以「後端 0.4 秒」跟「前端等滿 120 秒」可以同時成立。

**每一個 `fetch` 都要有上限。** 沒期限的請求不只是這次失敗，它會把 `DRAFT_BUSY`
這類旗標永遠留在設下的狀態，把後續全部擋掉。用 `tfetch(url, opts, ms)`。

### 6. 量測紀律

這個專案裡用區域／比例當代理指標，**已經誤導過三次**（兩次樂觀、一次悲觀）：

- 影片指標量到的是人物走過邊框，不是背景變化
- 「角落 alpha」在主體合法碰到角落時是偽陽性
- SAM 相減「角落乾淨了」其實是整個人被削掉——因為沒有同時量「主體有沒有活下來」

**結論落地前一定要把圖叫出來看。** 加指標時，一定要有一個對立面的指標
（去掉了多少 ↔ 留下了多少）。

### 9. ComfyUI 在別台時，它的 output 不在這台

審查抽幀、長片合併、封面、任務清單播放全都讀 `media_root` 裡的**本機檔案**。
ComfyUI 搬到別台之後每一條都會變成「影片不存在」。現在缺檔時 `media_fetch()` 會從
ComfyUI 的 `/view` 拉一份放進 `output/` 同一個相對路徑；任務完成時也會先在背景拉。
封面走 `media_thumb_remote()`，用 `/view?...&preview=jpeg;85`（實測小十倍）來縮。

媒體庫要看 ComfyUI 那台的全部檔案，只有 `/api/assets` 列得出來（`comfy_assets()`），
而且 **ComfyUI 啟動要加 `--enable-assets`**，沒加就回 404，媒體庫退回只列鏡像。
其他兩條都量過、不能用：`/internal/files/output` 用 `os.scandir` **不遞迴**
（3975 個檔只看得到根目錄的 177 個）；`/history` 只記得這次啟動後的工作。

- assets 的 `created_at` 是**被索引的時間**，第一次掃描全部同一秒，不能拿來排序。
  時間從路徑解析（`video/2026-09-22/004740_...`）；路徑裡沒有的跟 `/view` 打 HEAD
  拿 `Last-Modified`，存在 `thumbs/remote-mtime.json`。
- 縮圖一律由伺服器給。影片先找旁邊的 `-first-frame.png`／同名 `.png`，一張都沒有
  （約四分之一）才讓 ffmpeg 直接讀遠端 `/view`——它支援 Range，只抓得到開頭。
  **不要回到瀏覽器整支載影片再截圖**：那台有 35 GB。
- 前端一次只畫 240 張，捲到底（或點底下那條）再接下一批。四千張一次畫是三萬多個節點。
- `DELETE /api/assets/{id}` 只刪它資料庫的紀錄、不碰磁碟。這邊從來不呼叫它；
  媒體庫只刪得到這台的副本。

### 10. 反向代理後面的服務

- ComfyUI 走 https 時，websocket 必須包 TLS。以前一律送明文握手，對方直接斷線——
  **送單照常成功，只是進度條不動**，很容易以為是 ComfyUI 的問題。
- LLM 的代理只開放 `/v1/*`：`/props`、`/health`、`/slots` 是 nginx 回 401，
  `/v1/chat/completions` 正常。「測試連線」會退回 `/v1/models`。

### 11. SAM2 權重從 GitHub 下載只有 50-100 KB/s

154MB 要二十幾分鐘。以前在第一次推論時同步下載，而且握著服務的鎖——
**連 BiRefNet 去背都跟著卡死**，前端等滿 180 秒逾時。現在改成背景下載，
沒下載完時 `/segment` 立刻回 503 與目前的 MB 數。HuggingFace 上沒有官方鏡像。

### 7. 換行與編碼

`git` 會把工作區正規化成 **CRLF**。用 Python 打補丁時要讀檔案實際的換行：
`NL = "\r\n" if "\r\n" in s else "\n"`，否則多行比對全部落空。

主控台是 cp950，非 ASCII 會亂碼但不影響檔案內容。要看中文輸出就包
`io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")`。

bash heredoc 會吃掉一層跳脫（`\x89`、`\n` 都會被解讀），**長補丁請寫成 `.py` 檔再執行**，
不要用 heredoc。

### 8. 請求期限：預設就有，不要在呼叫端自己硬幹

這個毛病一支一支修過五次（matte、drafts、cutRun、sam3、chat）都沒收斂，
因為全檔 88 個 `fetch(` 裡只有 8 個有上限，而新功能持續加入新的裸 fetch。

現在在整段 script 最前面包了一次 `window.fetch`：預設 30 秒，要更久的
寫進 `BUDGET` 路由表。**要加長期限請改那張表，不要在呼叫端另外包。**

三類豁免：呼叫端自己帶了 `signal` 的（兩個期限疊起來短的那個會莫名贏）、
`data:`/`blob:`（不走網路）、以及表裡那幾條真的要跑幾分鐘的路線。

**規則：伺服器端的天花板要比前端的預算低。** 瀏覽器中止時，伺服器執行緒
**不會知道**——它還坐在 `urlopen` 裡等滿自己的上限，繼續吃上游服務。
改任一邊的數字時，另一邊要跟著改。

兩個容易踩的細節：

- `fetch` 的 promise 在「收到表頭」就 resolve，**不是讀完 body**。
  在 `finally` 裡 `clearTimeout` 等於只保護到表頭，後面的 `.json()`/`.blob()`
  又沒有上限了。抓全解析度 PNG 的那幾支正好踩在這個洞上。計時器留著就好。
- `<img src>` / `<video src>` **沒辦法在前端設期限**（AbortController 接不上），
  但它們同樣吃那六條連線。媒體庫的縮圖生不出來時**不可以退回原檔**——
  一頁二十張退成二十張全解析度影片會把連線卡死。現在改送 1x1 透明 PNG。

---

## 慣例

### commit 與 release

- **不加任何 Claude／Anthropic／collaborator 署名**。使用者明確要求過。
- commit 標題是**一句描述問題的話**，不是描述修法。看 `git log` 的體例。
- 分支 `feat/vX.Y` → 快轉進 `main` → 在該 commit 上打 tag。
- tag 內容是**雙語 release note**，每段先英文後中文。
- 打 tag 一定要 `--cleanup=verbatim`，否則 `#` 開頭的行會被吃掉。
- **只有使用者開口才 commit／push。**
- `git add` 要指名檔案，不要 `-A`——使用者常有自己未提交的修改在工作區。

### 寫程式

- 註解寫**為什麼**，不是寫在做什麼。特別是「這裡為什麼不能用比較直覺的那個寫法」。
- 前端是單一檔案，沒有建置流程。改完用
  `node --check`（把最大的 `<script>` 抽出來）驗語法。
- 後端只有標準函式庫，別加依賴。

---

## 目前進度（2026-09-24）

權威來源是 `git log` 與 GitHub releases。

已發佈到 **v2.5.1**。這台機器用 **Docker** 在跑（`h3-storyboard` 與 `h3-matte` 兩個容器），
llama-server 與 ComfyUI 都在別台，透過 https 反向代理連線。

### v2.5.1

- 媒體庫改用 ComfyUI 的 `/api/assets` 列出那台的全部成品（ComfyUI 要加 `--enable-assets`），
  封面由伺服器產生、檔案點開才下載、遠端獨有的檔案不能刪；一次只畫 240 張（見第 9 節）

### v2.5 做了什麼

**去背**

- 編輯器的**手動修補**：擦除／塗回原圖筆刷、復原（一筆一步，留六步）、重來
- **點選去除／點選補回**：點一下物件，SAM2 選整個物件（`/api/cutout/segment` 終於接上前端）
- **SAM3 person 閘**：模型下拉選單多一個 SAM3，跑 `workflows/SAM3_人物分離去背.json`，
  相乘在瀏覽器做（h3-server 沒有 numpy/cv2）
- 開啟編輯器**不再自動執行**，要按「▶ 執行」；換模型也不再偷偷重跑

**穩定性**

- **全域請求期限**（見第 8 節）
- log 改成非阻塞佇列；伺服器端七項逾時上限調整
- `withContext` 拿不到 Prompt 組時**中止**，不再帶著空的 system prompt 跑完還存進歷史
- `gpuPrepare` 把「逾時」與「端點掛掉」分開處理
- `start_app.bat` 會自己發現複製過來的 venv 跑不起來並重建

**部署**

- Docker compose（第 9-11 節是這一輪量到的坑）
- ComfyUI 在別台時的成品鏡像、https websocket、LLM `/props` 的退路
- 縮圖生不出來時卡片會寫「縮圖無法產生」，不再是一塊黑

### 下一步（使用者還沒交代，不要自己開始）

- 還沒在 Docker 版上實際送過一支影片到 ComfyUI（只驗證了既有任務的鏡像、封面與審查抽幀）
- 換 BiRefNet 權重（`BiRefNet-portrait` / `-matting`，MIT，885MB）——
  機制正確但全部在照片上訓練，沒有插畫上的公開評測；SAM3 已經夠用，暫不動

---

## 移機

看 `docs/DEPLOY.md`。三台機器分工（去背／LLM／ComfyUI）的設定也在那裡。
