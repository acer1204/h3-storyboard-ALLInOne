# H3 Storyboard — I2VA / FL2VA / L2VA / REF2VA Prompt Generator

A 20-second video+audio story-prompt generator for MiniMax H3 (Hailuo) video generation, powered by your own local llama-server (multimodal Qwen model).
以本地 llama-server（多模態 Qwen 模型）為後端的 20 秒影音劇情 Prompt 生成工具，輸出格式相容 MiniMax H3（Hailuo）影片生成。

Upload image(s), and the model looks at the characters, actions, expressions, props and environment, then writes a fun, coherent 20-second story prompt in English with Japanese dialogue.
上傳圖片後，模型會根據圖中的人物、動作、表情、物品與環境，自動編寫一段有趣且連貫的 20 秒英文劇情 Prompt，並附日文對話。

This project is fully open source — copy, modify and use it freely (MIT License).
本專案完全開源——可任意複製、修改與使用（MIT 授權）。

---

## Features 功能特色

### Four task modes 四種任務模式

- **I2VA** — the image is the FIRST frame; the story evolves forward from it.
  **I2VA**——圖片＝影片第一幀，劇情從這張圖向後展開。
- **FL2VA** — two images are the first and last frames; the model writes one continuous, logical transition between them.
  **FL2VA**——兩張圖＝首幀＋尾幀，模型編寫一段連貫合理的過渡劇情。
- **L2VA** — the image is the LAST frame; the story builds up and concludes exactly at it.
  **L2VA**——圖片＝影片最後一幀，劇情鋪陳並精準收束在這張圖。
- **REF2VA** — 1 to 9 reference images define character/object appearance (not frames); outputs the six-section reference format: `subject_definitions:` / `summary:` / `retention_analysis:` / `detailed_description:` / `overall_soundscape:` / `non_diegetic_music:`.
  **REF2VA**——1～9 張參考圖作為角色/物件的外觀依據（非影格），輸出六欄位參考格式。

### REF2VA details REF2VA 細節

- Uses `<Subject N>` / `<Picture N>` reference tokens; multiple views (front/back) of the same character are automatically merged into one subject.
  使用 `<Subject N>`／`<Picture N>` 代號；同一角色的多視角圖（正面/背面）會自動合併為同一個主體。
- Retention levels (`fully_preserved` / `attribute_transfer` / `weak_reference`) are stated per subject.
  每個主體標註保留等級（`fully_preserved`／`attribute_transfer`／`weak_reference`）。
- Every shot explicitly declares who is on screen and who is off-screen, preventing reference identities from bleeding into shots they don't belong to.
  每個 Shot 明確宣告哪些角色在畫面內、哪些完全不入鏡，防止參考身份滲漏（多出不該出現的人物）。
- Up to 3 shots are allowed, with `At 00:0X.000` timestamps from Shot 2 onward.
  最多允許 3 個鏡頭，第 2 鏡起使用 `At 00:0X.000` 時間戳。

### Story quality guarantees 劇情品質保證

- Dialogue uses the H3 syntax `(S1) says: <d>[Japanese] ...</d>`, and every line is followed by a lip-sync sentence.
  對話使用 H3 語法 `(S1) says: <d>[Japanese] ...</d>`，每句後面附唇形同步句。
- The main description is at least 300 English words, written as one continuous take with smooth type-first camera moves (push-in, pan, arc, crane, rack focus) and no hard cuts.
  主段落至少 300 個英文單詞，一鏡到底、平滑運鏡（推進/搖攝/環繞/升降/變焦），禁止硬切。
- Physical-continuity rules prevent common video artifacts: no teleporting motion, no "suddenly" limb movements, no hidden/invisible mechanisms, 2–4 deliberate action beats per 20 seconds.
  物理連續性規則防止常見影片破圖：禁止瞬移動作、禁止用「suddenly」描述肢體、禁止隱形機關，20 秒內只安排 2–4 個主要動作節拍。
- High randomness: the same image (and even the same hint) produces a different story every run, via temperature 1.0 + random seed + a randomly chosen creative angle.
  高隨機性：同一張圖（甚至同一個提示）每次生成的劇情都不同——temperature 1.0＋隨機 seed＋隨機抽選創意角度。

### Web UI 網頁介面

- Optional story hint (any language): leave it empty for free creation, or type a hint to make it the core of the plot.
  劇情提示（選填，任何語言）：留空＝自由創作；輸入提示＝以提示為劇情核心。
- Automatic format validation with auto-retry (up to 3 attempts) when the output misses any requirement.
  自動格式檢核，不達標自動重生成（最多 3 次）。
- History tab: every generation (including the uploaded images) is saved to the browser's IndexedDB — browse, copy, and delete entries.
  歷史紀錄分頁：每次生成（含上傳圖片）自動保存在瀏覽器 IndexedDB，可瀏覽、複製、刪除。
- Prompt tab: view, edit and save the System Prompt directly in the browser (stored in localStorage, with one-click reset to default).
  Prompt 分頁：直接在網頁檢視、編輯並保存 System Prompt（存於 localStorage，可一鍵還原預設）。
- Images are compressed client-side (longest edge 1024px) before upload for faster requests.
  圖片會在瀏覽器端先壓縮（長邊 1024px）再上傳，加快請求速度。

---

## Getting started 快速開始

1. Run [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server` with a multimodal Qwen model (tested with a 27B Q4_K_M build) and the OpenAI-compatible API enabled.
   在任一台機器用 [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server` 載入多模態 Qwen 模型（測試使用 27B Q4_K_M），開啟 OpenAI 相容 API。
2. Double-click `start_app.bat` — it creates a dedicated Python venv on first run and serves the app at `http://localhost:7766/app.html`.
   雙擊 `start_app.bat`——首次執行會自動建立專屬 Python venv，並在 `http://localhost:7766/app.html` 啟動前端。
3. Enter your llama-server address in the "伺服器" field on the page.
   在頁面的「伺服器」欄位填入你的 llama-server 位址。
4. Pick a mode, upload image(s), optionally type a story hint, then click Generate.
   選擇模式 → 上傳圖片 →（選填）輸入劇情提示 → 按「生成」。

Requirements: Windows + Python 3, and a modern browser. The frontend is a single HTML file with no external dependencies.
需求：Windows＋Python 3 與現代瀏覽器。前端為單一 HTML 檔，無任何外部相依。

---

## Files 檔案說明

| File 檔案 | Purpose 用途 |
|---|---|
| `app.html` | The whole frontend in one HTML file. 前端（單一 HTML 檔）。 |
| `start_app.bat` | One-click launcher: creates the venv and serves on port 7766. 一鍵啟動：建立 venv 並在 port 7766 服務。 |
| `i2va_test.py` | The master System Prompt + a batch test harness. System Prompt 主版本＋批次測試腳本。 |
| `sync_prompt.py` | Syncs the System Prompt from `i2va_test.py` into `app.html`. 將 System Prompt 從 `i2va_test.py` 同步進 `app.html`。 |

Recommended prompt-editing flow: edit `SYSTEM_PROMPT` in `i2va_test.py`, then run `python sync_prompt.py` so both copies stay identical.
建議的 Prompt 修改流程：編輯 `i2va_test.py` 裡的 `SYSTEM_PROMPT`，再執行 `python sync_prompt.py`，保持兩邊一致。

---

## License 授權

MIT License — free to copy, modify, distribute and use for any purpose.
MIT 授權——可為任何目的自由複製、修改、散布與使用。
