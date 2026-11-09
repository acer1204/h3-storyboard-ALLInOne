# -*- coding: utf-8 -*-
"""
H3 Prompt 批次產生器 - 本機服務

提供頁面本身，外加一組歷史紀錄 API。紀錄存在 ./history/ 底下，
所以區網上任何裝置連進來看到的都是同一份。

  GET    /                      -> h3-batch-tester.html
  GET    /api/history           -> 索引（新的在前）
  GET    /api/history/<id>      -> 單筆完整內容
  GET    /api/thumb/<id>.jpg    -> 縮圖
  GET    /api/full/<id>.jpg     -> 送去模型的原圖（重跑用）
  POST   /api/history           -> 新增一筆，回 {"id": ...}
  DELETE /api/history/<id>      -> 刪一筆
  POST   /api/history/clear     -> 全部清掉

  POST   /api/comfy/run         -> 送出生成：換圖 + 三欄位，其餘照模板
  GET    /api/comfy/status/<id> -> 查佇列/執行/完成狀態
  POST   /api/comfy/refresh     -> 以 ComfyUI 最近一次成功生成更新模板
  POST   /api/comfy/template    -> 直接上傳 workflow JSON 當模板（ComfyUI 選單 Export (API) 的檔案）
  GET    /api/comfy/params      -> 模板目前的工作流參數（FPS/解析度/步數/shift）與可選清單

  GET    /api/media             -> 媒體庫檔案清單（ComfyUI output）
  GET    /api/media/file/<path> -> 取檔（支援 Range，?dl=1 強制下載）
  DELETE /api/media/file/<path> -> 刪除檔案

  GET    /api/prompts           -> system prompt 清單
  GET    /api/prompts/<id>      -> 單一 prompt 全文
  POST   /api/prompts           -> 新增或更新 {id?, name, text, mode?, overlay?}（overlay=疊加在官方組之上）
  DELETE /api/prompts/<id>      -> 刪除

  GET    /api/uploads           -> 上傳過的原圖清單（以內容 hash 去重）
  GET    /api/uploads/<hash>.jpg        -> 1024px 工作副本
  GET    /api/uploads/<hash>.orig.<ext> -> 全解析度原圖（送 ComfyUI 用）
  GET    /api/orig/<id>.<ext>           -> 歷史紀錄的全解析度原圖
  POST   /api/history/<id>/rate -> 評分 {rating: up|down|"", fb_tags:[...], fb_note:"..."}
  GET    /api/habits?mode=cuts|onetake  -> 慣性偵測：跨不同圖片高頻出現的動作片語
  GET    /api/lessons           -> 經驗庫清單
  POST   /api/lessons           -> 新增/更新 {id?, en, zh, mode, enabled, ban, src}
  DELETE /api/lessons/<id>      -> 刪除
  GET    /api/lessons/pending   -> 已評分、尚未整理的反饋（給「整理經驗」用）
  POST   /api/lessons/mark      -> {ids:[...]} 把反饋標成已整理
  GET    /api/lessons/required  -> 經驗庫必要規則（結構門檻，永遠套用、不可刪除）
  POST   /api/lessons/required  -> 更新必要規則 {min_words, lang_lock, line_min, line_max}（lang_lock：有台詞就必須是這語言，沒台詞不算違規）
  GET    /api/uploads/<hash>.thumb.jpg  -> 縮圖
  DELETE /api/uploads/<hash>              -> 只從上傳庫移除（歷史保留、upload_id 保留；同圖再上傳會自動歸位）
  DELETE /api/uploads/<hash>?cascade=1    -> 連同所有引用它的歷史紀錄一起刪
  GET    /api/uploads/<hash>/history    -> 引用這張圖的歷史紀錄 id 清單（刪除前確認用）

  GET    /api/loras             -> LoRA 觸發詞預設清單
  GET    /api/loras/<id>        -> 單一預設 {id, name, main, subs:[{key,gloss}]}
  POST   /api/loras             -> 新增或更新 {id?, name, main, subs}
  DELETE /api/loras/<id>        -> 刪除
"""
import argparse, base64, hashlib, io, json, os, queue, re, socket, struct, subprocess, sys, threading, time, uuid
import urllib.request
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, unquote, parse_qs, quote, urlencode

ROOT = os.path.dirname(os.path.abspath(__file__))
# ---- 關於：版本與開源資訊 ----
# 發佈新版時要跟著改，而且要跟 h3-webui.html 的 H3_VERSION 一致——「關於」頁會比對兩邊，
# 不一致就提示瀏覽器還拿著舊網頁（伺服器沒有關掉快取，換版後常常要 Ctrl+F5）。
APP_VERSION = "2.6"
APP_NAME = "H3 Storyboard"
APP_AUTHOR = "acer1204"
APP_REPO = "acer1204/h3-storyboard-ALLInOne"
APP_REPO_URL = "https://github.com/" + APP_REPO
APP_LICENSE = "MIT"
HIST = os.path.join(ROOT, "history")
UPLOADS = os.path.join(ROOT, "uploads")
UINDEX = os.path.join(UPLOADS, "index.json")
INDEX = os.path.join(HIST, "index.json")
PROMPTS = os.path.join(ROOT, "prompts")
PINDEX = os.path.join(PROMPTS, "index.json")
LORAS = os.path.join(ROOT, "loras")
LINDEX = os.path.join(LORAS, "index.json")
MOVIES = os.path.join(ROOT, "movies")
QUEUE = os.path.join(ROOT, "queue")        # 任務佇列：唯一真相（ComfyUI /history 重啟就清空）
QINDEX = os.path.join(QUEUE, "index.json")
SETTINGS_PATH = os.path.join(ROOT, "settings.json")   # 共用設定（跨瀏覽器／跨電腦一致）
DRAFTS_PATH = os.path.join(ROOT, "drafts.json")       # 未送出的工作（F5／換電腦都能接續）
MINDEX = os.path.join(MOVIES, "index.json")
CONTINUES = os.path.join(ROOT, "continues")   # I2VA Continue 接龍專案
CINDEX = os.path.join(CONTINUES, "index.json")
LESSONS_DIR = os.path.join(ROOT, "lessons")
LESSONS_FILE = os.path.join(LESSONS_DIR, "lessons.json")
REQUIRED_FILE = os.path.join(LESSONS_DIR, "required.json")
os.makedirs(LESSONS_DIR, exist_ok=True)
SKILLS_DIR = os.path.join(ROOT, "skills")   # 官方 MiniMax H3 skills（隨 repo 附帶）
PAGE = "h3-webui.html"
MODES = ("t2va", "i2va", "fl2va", "l2va", "ref2va")   # generation task modes
# ---------------------------------------------------------------- config
# Personal hosts/paths live in config.json (git-ignored). config.example.json documents the keys.
# Priority: CLI flag > env var H3_* > config.json > built-in default.
CONFIG_PATH = os.path.join(ROOT, "config.json")
CONFIG_DEFAULTS = {
    "llama_url":  "http://127.0.0.1:8080",          # llama-server (llama.cpp) with a vision model
    "comfy_url":  "http://127.0.0.1:8188",          # ComfyUI API
    "media_root": os.path.join(ROOT, "output"),     # folder the media view browses (usually ComfyUI/output)
    "workflow_dir": os.path.join(ROOT, "workflows"),  # folder holding ComfyUI workflow .json files
    "workflow_current": "",                          # currently selected workflow filename
    "gpu_free_mb": 4000,                             # GPU considered "free" below this used-MB threshold
    "bind":       "0.0.0.0",
    "port":       9998,
}


def load_config():
    cfg = dict(CONFIG_DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user = json.load(f)
        if isinstance(user, dict):
            cfg.update({k: v for k, v in user.items() if k in CONFIG_DEFAULTS and v not in (None, "")})
    except FileNotFoundError:
        pass
    except Exception as e:
        print("[WARN] config.json unreadable (%s) - using defaults" % e)
    for k in CONFIG_DEFAULTS:
        ev = os.environ.get("H3_" + k.upper())
        if ev:
            cfg[k] = int(ev) if k == "port" else ev
    return cfg


CONFIG = load_config()
MEDIA_ROOT = CONFIG["media_root"]
COMFY_URL = CONFIG["comfy_url"].rstrip("/")
# 上次成功跟 ComfyUI 說上話是什麼時候。判「問不到」到底是
# 「沒在跑」還是「忙到不回話」時要用——這台機器上沒開的本機埠
# 會 timeout 而不是 RST，兩者在例外層級分不出來。
COMFY_SEEN = [0.0]

_CFG_MTIME = [os.path.getmtime(CONFIG_PATH) if os.path.exists(CONFIG_PATH) else 0]

# llama 使用追蹤（GPU 協調用）：所有 llama 呼叫都走 /api/llama/chat 代理，
# 所以伺服器能精確知道「誰在用 GPU」——in-flight 計數＋最後一次完成時間
LLAMA_INFLIGHT = [0]
LLAMA_LAST = [0.0]


def maybe_reload_config():
    """config.json 被手動編輯後自動重讀（以 mtime 判斷），改位址不需要重啟伺服器。"""
    global CONFIG, MEDIA_ROOT, COMFY_URL
    try:
        mt = os.path.getmtime(CONFIG_PATH)
    except OSError:
        return
    if mt == _CFG_MTIME[0]:
        return
    _CFG_MTIME[0] = mt
    CONFIG = load_config()
    MEDIA_ROOT = CONFIG["media_root"]
    COMFY_URL = CONFIG["comfy_url"].rstrip("/")
    print("[config] config.json 已重新載入")


def save_config():
    """Persist the editable keys to config.json and refresh module globals.

    同一份 config.json 會被 Windows 上的 start_app.bat 與 docker 容器輪流讀。
    等於內建預設的值寫成空字串（讀回來就是當下環境的預設：F:/.../workflows
    或 /app/workflows），環境變數給的值則不寫回去——否則在容器裡按一次
    儲存，就把 /app/... 這種路徑寫進檔案，換回 bat 啟動時整個指錯。"""
    global MEDIA_ROOT, COMFY_URL
    MEDIA_ROOT = CONFIG["media_root"]
    COMFY_URL = CONFIG["comfy_url"].rstrip("/")
    keep = {}
    for k in CONFIG_DEFAULTS:
        if k in ("bind", "port") or os.environ.get("H3_" + k.upper()):
            continue
        keep[k] = "" if CONFIG[k] == CONFIG_DEFAULTS[k] else CONFIG[k]
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cur = json.load(f)
        if isinstance(cur, dict):
            cur.update(keep)
            keep = cur
    except Exception:
        pass
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(keep, f, ensure_ascii=False, indent=1)


def gpu_mem():
    """Query local GPU memory via nvidia-smi. Returns {used_mb,total_mb} or {error}."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            timeout=10).decode("utf-8", "replace")
        used, total = [int(x.strip()) for x in out.strip().splitlines()[0].split(",")]
        return {"used_mb": used, "total_mb": total}
    except Exception as e:
        return {"error": str(e)}


def comfy_busy():
    """True＝在算圖或有排隊；False＝確定不忙；None＝問不到且可疑。

    None 會讓去背被擋，所以不能隨便回——根本沒在跑 ComfyUI 的人
    不該因此永遠不能去背。本機沒開的埠在這台機器上是 timeout
    而不是連線被拒，所以分不出來；改用「之前聯絡上過嗎」判：
    剛還好好的、現在不回話＝忙；從來沒聯絡上＝沒在跑。
    閒置時 /queue 實測 1-2 ms，算圖中也只有 3-19 ms，2 秒是百倍餘裕。"""
    try:
        q = comfy_api("/queue", timeout=2)
        return bool(q.get("queue_running")) or bool(q.get("queue_pending"))
    except Exception:
        if COMFY_SEEN[0] and time.time() - COMFY_SEEN[0] < 600:
            return None                # 不久前還在，現在答不出來＝可疑
        return False                   # 從來沒聯絡上＝沒在跑，不該擋去背


def comfy_free():
    """Ask ComfyUI to unload models and free VRAM."""
    return comfy_api("/free", {"unload_models": True, "free_memory": True}, timeout=20)


def find_ffmpeg():
    """尋找 ffmpeg：PATH → ComfyUI 安裝目錄下任何 Python 環境（含 .env 等點開頭資料夾，
    glob 預設跳過點開頭所以改用 listdir）附帶的 imageio_ffmpeg 執行檔。"""
    import shutil, glob
    p = shutil.which("ffmpeg")
    if p:
        return p
    seen = set()
    for updepth in (2, 3):    # media_root 通常是 <根>/ComfyUI/output → 根在上兩層
        base = os.path.abspath(os.path.join(CONFIG["media_root"], *[".."] * updepth))
        if base in seen or not os.path.isdir(base):
            continue
        seen.add(base)
        try:
            subs = os.listdir(base)
        except OSError:
            continue
        for sub in subs:
            hits = glob.glob(os.path.join(base, sub, "Lib", "site-packages",
                                          "imageio_ffmpeg", "binaries", "ffmpeg-*.exe"))
            if hits:
                return hits[0]
    return None


def _gray_transition_scan(ff, ap, fps=8.0, w=64, h=36):
    """硬切檢測第一層 B：低解析灰階幀序列分析。
    逐格差異峰值抓「瞬間硬切」；跨距（~0.9 秒）差異峰值抓「漸進轉場」——
    wipe（劃像）逐格只動一條窄帶、單格分數永遠不高，ffmpeg scene 偵測抓不到，
    但跨距前後兩張圖幾乎完全不同；再用直欄集中度分辨 wipe 與 dissolve。
    回傳 [{t, score, type: cut|wipe|transition}]。"""
    import subprocess as sp
    import statistics
    r = sp.run([ff, "-i", ap, "-vf", "fps=%g,scale=%d:%d,format=gray" % (fps, w, h),
                "-f", "rawvideo", "-"], capture_output=True, timeout=180)
    raw = r.stdout
    fsz = w * h
    n = len(raw) // fsz
    if n < 6:
        return []
    fr = [raw[i * fsz:(i + 1) * fsz] for i in range(n)]

    def mad(a, b):
        s = 0
        for x, y in zip(a, b):
            s += x - y if x >= y else y - x
        return s / (fsz * 255.0)

    d1 = [mad(fr[i], fr[i + 1]) for i in range(n - 1)]
    gap = max(2, int(fps * 0.9))
    dg = [mad(fr[i], fr[i + gap]) for i in range(n - gap)]
    med1 = statistics.median(d1) or 1e-6
    medg = statistics.median(dg) or 1e-6
    events = []
    # 瞬間硬切：單格差異遠高於本片自身的運動基準
    for i, v in enumerate(d1):
        if v > max(0.12, 5.0 * med1) and v == max(d1[max(0, i - 2):i + 3]):
            events.append({"t": round((i + 1) / fps, 2), "score": round(v, 3), "type": "cut", "src": "gray"})
    # 漸進轉場：跨距差異連續超標的區段（wipe / dissolve / fade）
    thr = max(0.15, 3.0 * medg)
    i = 0
    while i < len(dg):
        if dg[i] <= thr:
            i += 1
            continue
        j = i
        while j < len(dg) and dg[j] > thr:
            j += 1
        k = max(range(i, j), key=lambda x: dg[x])
        tc = (k + gap / 2.0) / fps
        if not any(abs(e["t"] - tc) < gap / fps for e in events):
            conc = 0.0        # 變化量集中在少數直欄 = 有移動分界線 = wipe
            for s in range(k, min(k + gap, n - 1)):
                cols = [0] * w
                a, b = fr[s], fr[s + 1]
                for y in range(h):
                    row = y * w
                    for x in range(w):
                        d = a[row + x] - b[row + x]
                        cols[x] += d if d >= 0 else -d
                tot = sum(cols) or 1
                conc = max(conc, sum(sorted(cols, reverse=True)[:max(1, w // 5)]) / tot)
            events.append({"t": round(tc, 2), "score": round(dg[k], 3),
                           "type": "wipe" if conc > 0.5 else "transition",
                           "conc": round(conc, 2), "src": "gray"})
        i = j
    return events


def review_scan(rel, scene_thr=0.30, max_frames=24):
    """硬切檢測第一層：ffmpeg 場景偵測＋灰階幀序列分析（瞬切與 wipe/dissolve 漸進轉場），
    再抽幀（等距＋候選點附近密集＋最後一幀）。
    回傳 {duration, cuts:[{t,score,type}], frames:[{t,b64}]}；b64 為 448px JPEG。"""
    import subprocess as sp
    import base64 as b64mod
    ff = find_ffmpeg()
    if not ff:
        raise ValueError("找不到 ffmpeg")
    media_fetch(rel)          # ComfyUI 在別台時先把成品拉回來
    ap = os.path.abspath(os.path.join(MEDIA_ROOT, rel.replace("\\", "/")))
    if not ap.startswith(os.path.abspath(MEDIA_ROOT)) or not os.path.exists(ap):
        raise ValueError("影片不存在或路徑不合法: %s" % rel)
    # 1) 場景偵測 + 時長
    r = sp.run([ff, "-i", ap, "-vf", "select='gt(scene,%s)',metadata=print:file=-" % scene_thr,
                "-an", "-f", "null", "-"], capture_output=True, timeout=180)
    out = r.stdout.decode("utf-8", "replace") + "\n" + r.stderr.decode("utf-8", "replace")
    dur = 0.0
    md = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", out)
    if md:
        dur = int(md.group(1)) * 3600 + int(md.group(2)) * 60 + float(md.group(3))
    cuts, cur_t = [], None
    for line in out.splitlines():
        mt = re.search(r"pts_time:([0-9.]+)", line)
        if mt:
            cur_t = float(mt.group(1))
        ms = re.search(r"lavfi\.scene_score=([0-9.]+)", line)
        if ms and cur_t is not None:
            cuts.append({"t": round(cur_t, 2), "score": round(float(ms.group(1)), 3), "type": "cut", "src": "scene"})
            cur_t = None
    # 1b) 灰階序列分析：瞬切與 wipe/dissolve。以灰階候選為主（尺度一致、帶 wipe 特徵，
    # 供前端高確信強制規則使用）；scene 候選只在附近沒有灰階候選時補進來。
    try:
        gray = _gray_transition_scan(ff, ap)
        cuts = gray + [c for c in cuts if not any(abs(g["t"] - c["t"]) < 0.7 for g in gray)]
    except Exception:
        pass
    cuts = sorted(cuts, key=lambda c: c["t"])[:8]
    # 2) 抽幀時間點：等距 10 張 + 每個切點 ±0.6/±0.2 秒 + 最後一幀
    times = set()
    n_even = 10
    if dur > 0.5:
        for k in range(n_even):
            times.add(round(0.2 + (dur - 0.4) * k / max(1, n_even - 1), 2))
        for c in cuts:
            # 漸進轉場（wipe/dissolve）跨度約 1 秒，要取更寬的前後幀才能看出前後不連貫
            offs = (-0.6, -0.2, 0.2, 0.6) if c.get("type") == "cut" else (-1.1, -0.6, 0.2, 0.6, 1.1)
            for dt in offs:
                t = c["t"] + dt
                if 0 <= t <= dur - 0.05:
                    times.add(round(t, 2))
        times.add(round(max(0.0, dur - 0.08), 2))
    times = sorted(times)[:max_frames]
    frames = []
    # 下面那個 except 會把 TimeoutExpired 一起吞掉，所以 24 張全卡住時
    # 每張都燒滿自己的上限而不會提早跳出——整個請求因此可以拖到十幾分鐘。
    # 改成整段一個牆鐘預算：超過就拿已經抓到的回去，有幾張算幾張。
    t_budget = time.time() + 60
    for t in times:
        if time.time() > t_budget:
            slog("  [scan] 抽幀超過 60 秒，已取得 %d/%d 張，先回傳" % (len(frames), len(times)))
            break
        try:
            fr = sp.run([ff, "-ss", str(t), "-i", ap, "-frames:v", "1",
                         "-vf", "scale=448:-2", "-q:v", "7", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                        capture_output=True, timeout=10)
            if fr.stdout:
                frames.append({"t": t, "b64": b64mod.b64encode(fr.stdout).decode()})
        except Exception:
            continue
    return {"duration": round(dur, 2), "cuts": cuts, "frames": frames}


def load_mindex():
    try:
        with open(MINDEX, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_mindex(rows):
    os.makedirs(MOVIES, exist_ok=True)
    with open(MINDEX, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)


def load_cindex():
    try:
        with open(CINDEX, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _atomic_json(fp, obj):
    """專案檔每一步都會重寫。寫到一半斷掉的話整個接龍（可能是幾小時的算圖）就讀不回來了。"""
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    # Windows：目標檔剛好被讀取中時 replace 會失敗，等一下就好
    for _try in range(5):
        try:
            os.replace(tmp, fp)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(tmp, fp)


def _s(v, n):
    return str(v if v is not None else "")[:n]


def _i(v, lo, hi, d=0):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return d


def _f(v):
    try:
        return max(0.0, float(v or 0))
    except (TypeError, ValueError):
        return 0.0


def ic_snap(x):
    """這一章實際送出的 LoRA。配方事後可能被改掉或刪掉，還原時要用得回當時那一份。"""
    if not isinstance(x, dict):
        return None
    rows = []
    for r in (x.get("loras") or [])[:15]:
        if isinstance(r, dict) and r.get("lora"):
            try:
                st = float(r.get("str", 1))
            except (TypeError, ValueError):
                st = 1.0
            rows.append({"lora": _s(r["lora"], 300), "str": st})
    return {"recipe": _s(x.get("recipe"), 40), "name": _s(x.get("name"), 80),
            "loras": rows, "prompt": _s(x.get("prompt"), 1200)}


def ic_chapter(c):
    if not isinstance(c, dict):
        c = {}
    takes = []
    for t in (c.get("takes") or [])[:30]:
        if isinstance(t, dict) and t.get("video"):
            takes.append({"video": _s(t["video"], 300), "ts": _s(t.get("ts"), 40),
                          "content": str(t.get("content") or ""), "snap": ic_snap(t.get("snap")),
                          "parent": _s(t.get("parent"), 300),
                          "last_frame": _s(t.get("last_frame"), 300), "cut": _f(t.get("cut")),
                          "face_rejected": bool(t.get("face_rejected"))})
    return {"beats": _s(c.get("beats"), 3000), "dialogue": _s(c.get("dialogue"), 1000),
            "ends_on": _s(c.get("ends_on"), 500), "hint": _s(c.get("hint"), 2000),
            "recipe": _s(c.get("recipe"), 40), "use_snap": bool(c.get("use_snap")),
            "snap": ic_snap(c.get("snap")),
            "content": str(c.get("content") or ""), "zh": str(c.get("zh") or ""),
            "edited": bool(c.get("edited")), "stale": bool(c.get("stale")),
            "state": _s(c.get("state"), 10), "attempts": _i(c.get("attempts"), 0, 99),
            "dur_gen": _i(c.get("dur_gen"), 0, 120),
            "pid": _s(c.get("pid"), 60), "video": _s(c.get("video"), 300), "takes": takes,
            # 真正送出去的文字與它接的前一章影片。content 之後可能被編輯，不能當成這支影片的 prompt
            "sent": str(c.get("sent") or ""), "parent": _s(c.get("parent"), 300), "hard": bool(c.get("hard")),
            "vstate": _s(c.get("vstate"), 12),
            # 結尾沒有臉時改接的畫面與它的秒數；合併時要剪到這裡
            "last_frame": _s(c.get("last_frame"), 300), "cut": _f(c.get("cut")),
            "face_checked": bool(c.get("face_checked")), "face_skip": bool(c.get("face_skip")),
            "face_fail": bool(c.get("face_fail")), "face_tries": _i(c.get("face_tries"), 0, 20),
            "v_e": [_s(x, 300) for x in (c.get("v_e") or [])][:20],
            "v_w": [_s(x, 300) for x in (c.get("v_w") or [])][:20],
            "error": _s(c.get("error"), 400)}


def ic_load(rid):
    with open(os.path.join(CONTINUES, rid + ".json"), encoding="utf-8") as f:
        return json.load(f)


def ic_commit(rec):
    """寫回專案檔與索引。呼叫端要握著 LOCK。rev 讓網頁知道內容變了，才不用每次都抓整份。"""
    rec["rev"] = int(rec.get("rev") or 0) + 1
    rec["up_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _atomic_json(os.path.join(CONTINUES, rec["id"] + ".json"), rec)
    row = {"id": rec["id"], "ts": rec.get("ts"), "up_ts": rec["up_ts"], "title": rec.get("title"),
           "target": rec.get("target"), "seg": rec.get("seg"), "n_ch": len(rec["chapters"]),
           "n_done": sum(1 for c in rec["chapters"] if c["video"]),
           "status": rec.get("status"), "final": rec.get("final"), "open": rec.get("open"), "rev": rec["rev"]}
    rows = load_cindex()
    for k, r in enumerate(rows):
        if r.get("id") == rec["id"]:
            rows[k] = row
            break
    else:
        rows.insert(0, row)
    _atomic_json(CINDEX, rows)


def ic_update(rid, **fields):
    with LOCK:
        rec = ic_load(rid)
        rec.update(fields)
        ic_commit(rec)
    return rec


def ic_ch(rid, i, **fields):
    with LOCK:
        rec = ic_load(rid)
        rec["chapters"][i].update(fields)
        ic_commit(rec)
    return rec


class IcConflict(Exception):
    def __init__(self, rec):
        super().__init__("conflict")
        self.rec = rec


def ic_save(body):
    """建立／更新一個接龍專案，回傳 (id, rev)。圖片只在建立時帶。"""
    rid = str(body.get("id") or "")
    if rid and not ID_RE.match(rid):
        raise ValueError("bad id")
    rid = rid or new_id()
    # 跑的時候章節由背景那條線負責。這時收網頁的存檔，會把它剛寫進去的 pid 與影片蓋回舊的。
    if ic_active(rid):
        return rid, None
    img, ext = parse_data_image(body.pop("image", None))
    thumb, _t = parse_data_image(body.pop("thumb", None))
    fp = os.path.join(CONTINUES, rid + ".json")
    plan = body.get("plan") if isinstance(body.get("plan"), dict) else {}
    chs = body.get("chapters") if isinstance(body.get("chapters"), list) else []
    rec = {"id": rid, "title": _s(body.get("title"), 80), "story": _s(body.get("story"), 6000),
           "target": _i(body.get("target"), 1, 3600), "seg": _i(body.get("seg"), 1, 120, 10),
           "n": _i(body.get("n"), 0, IC_MAX_CHAPTERS),
           "aspect": _s(body.get("aspect") or "auto", 12), "recipe": _s(body.get("recipe"), 40),
           "w": _i(body.get("w"), 0, 20000), "h": _i(body.get("h"), 0, 20000),
           "status": _s(body.get("status") or "new", 20), "final": _s(body.get("final"), 300),
           "msg": _s(body.get("msg"), 300), "face_guard": bool(body.get("face_guard", True)),
           "plan": {k: _s(plan.get(k), 2000) for k in
                    ("story", "style", "identity", "voice", "setting", "soundscape", "music")},
           "chapters": [ic_chapter(c) for c in chs[:IC_MAX_CHAPTERS]]}
    with LOCK:
        os.makedirs(CONTINUES, exist_ok=True)
        old = {}
        try:
            with open(fp, encoding="utf-8") as f:
                old = json.load(f)
        except Exception:
            pass
        # 整條鏈是在網頁關著的時候跑完的，留著的舊分頁手上還是沒有影片的版本。
        # 它一存檔就會把成果整份蓋掉，所以要比對版本。
        if old and body.get("base_rev") is not None and _i(body.get("base_rev"), 0, 2 ** 31) != int(old.get("rev") or 0):
            raise IcConflict(old)
        # 這幾個狀態只有背景那條線能設。存進來的話，下次重啟會把它當成跑到一半的接回去。
        if rec["status"] in ("queued", "running", "planning"):
            rec["status"] = "paused"
        if img:
            with open(os.path.join(CONTINUES, "%s.img.%s" % (rid, ext or "jpg")), "wb") as f:
                f.write(img)
        if thumb:
            with open(os.path.join(CONTINUES, rid + ".thumb.jpg"), "wb") as f:
                f.write(thumb)
        rec["ts"] = old.get("ts") or time.strftime("%Y-%m-%d %H:%M:%S")
        rec["rev"] = int(old.get("rev") or 0)
        rec["open"] = bool(body["open"]) if "open" in body else bool(old.get("open", True))
        ic_commit(rec)
    return rid, rec["rev"]


def _concat_trim(ff, abses, cuts, deadline, outdir):
    """有章節要剪結尾時只能重新編碼：-c copy 只能切在關鍵幀上，對不到接下一章的那一幀。"""
    infos = [ic_video_info(ff, ap) for ap in abses]
    audio = all(x[1] for x in infos)
    sizes = {x[2] for x in infos if x[2]}
    if len(sizes) > 1:
        raise ValueError("各章畫面大小不同（%s），沒辦法合併。通常是中途換了解析度或模板" % "、".join(sorted(sizes)))
    args, parts, labels = [ff, "-y", "-hide_banner", "-loglevel", "error"], [], ""
    for k, ap in enumerate(abses):
        args += ["-i", ap]
        t = _f(cuts[k]) if k < len(cuts) else 0.0
        parts.append("[%d:v]%ssetpts=PTS-STARTPTS,setsar=1[v%d]" % (k, ("trim=end=%.3f," % t) if t else "", k))
        labels += "[v%d]" % k
        if audio:
            parts.append("[%d:a]%sasetpts=PTS-STARTPTS[a%d]" % (k, ("atrim=end=%.3f," % t) if t else "", k))
            labels += "[a%d]" % k
    parts.append("%sconcat=n=%d:v=1:a=%d[v]%s" % (labels, len(abses), 1 if audio else 0, "[a]" if audio else ""))
    out = os.path.join(outdir, "movie_%s_%dseg.mp4" % (time.strftime("%Y%m%d_%H%M%S"), len(abses)))
    args += ["-filter_complex", ";".join(parts), "-map", "[v]"] + (["-map", "[a]", "-c:a", "aac", "-b:a", "192k"] if audio else [])
    part = out + ".part"              # 失敗不留壞檔：副檔名不是影片，媒體庫也不會列出來
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-f", "mp4", part]
    try:
        r = subprocess.run(args, capture_output=True, timeout=max(30, deadline - time.time()))
        if r.returncode != 0:
            raise ValueError("ffmpeg 失敗: " + r.stderr.decode("utf-8", "replace")[:400])
        os.replace(part, out)
    except subprocess.TimeoutExpired:
        raise ValueError("影片太長，時限內編不完。有章節要剪結尾時整部片都要重新編碼")
    finally:
        try:
            os.unlink(part)
        except OSError:
            pass
    return os.path.relpath(out, MEDIA_ROOT).replace("\\", "/")


def movie_concat(rel_files, cuts=None):
    """把多段 mp4（media_root 相對路徑）用 ffmpeg concat demuxer 無損串接成一部長片。
    回傳輸出檔的 media_root 相對路徑。"""
    import subprocess as sp
    import tempfile
    ff = find_ffmpeg()
    if not ff:
        raise ValueError("找不到 ffmpeg（PATH 或 ComfyUI 環境內都沒有）")
    abses = []
    # 下載與 ffmpeg 共用一個期限，要比前端的 330 秒短（瀏覽器中止時這裡不會知道）。
    # 來不及就先在背景拉，讓使用者晚點再按，不要每支各等滿 180 秒。
    deadline = time.time() + 300
    for k, rf in enumerate(rel_files):
        left = deadline - time.time() - 30
        if left < 5:
            media_prefetch(*rel_files[k:])
            raise ValueError("還有 %d 支片段在下載，稍等一下再按一次合併" % (len(rel_files) - k))
        media_fetch(rf, budget=min(180, int(left)))
        ap = os.path.abspath(os.path.join(MEDIA_ROOT, rf.replace("\\", "/")))
        if not ap.startswith(os.path.abspath(MEDIA_ROOT)) or not os.path.exists(ap):
            raise ValueError("片段不存在或路徑不合法: %s" % rf)
        abses.append(ap)
    cuts = cuts if isinstance(cuts, list) else []
    if len(abses) < 2 and not any(cuts):
        raise ValueError("至少要兩段影片")
    outdir = os.path.join(MEDIA_ROOT, "video", "movies")
    os.makedirs(outdir, exist_ok=True)
    if any(cuts):
        return _concat_trim(ff, abses, cuts, deadline, outdir)
    # 容器要跟片段一致：把 WebM(VP9/Opus) 用 -c copy 塞進 .mp4 會失敗
    _ext = os.path.splitext(abses[0])[1].lower() or ".mp4"
    if _ext not in VIDEO_EXT: _ext = ".mp4"
    out = os.path.join(outdir, "movie_%s_%dseg%s" % (time.strftime("%Y%m%d_%H%M%S"), len(abses), _ext))
    lst = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    try:
        for ap in abses:
            lst.write("file '%s'\n" % ap.replace("'", "'\\''"))
        lst.close()
        # 寫到 .part，成功才改名：失敗或逾時留下的半成品不該出現在媒體庫
        fmt = {".mp4": "mp4", ".webm": "webm", ".mkv": "matroska", ".mov": "mov"}[_ext]
        r = sp.run([ff, "-y", "-f", "concat", "-safe", "0", "-i", lst.name, "-c", "copy", "-f", fmt, out + ".part"],
                   capture_output=True, timeout=max(30, deadline - time.time()))
        if r.returncode != 0:
            raise ValueError("ffmpeg 失敗: " + r.stderr.decode("utf-8", "replace")[-400:])
        os.replace(out + ".part", out)
    finally:
        for f0 in (lst.name, out + ".part"):
            try:
                os.unlink(f0)
            except OSError:
                pass
    return os.path.relpath(out, MEDIA_ROOT).replace("\\", "/")
COMFY_TEMPLATE = os.path.join(ROOT, "comfy-template.json")
# v18 的 DaSiWa_EnhancedVideoCombine 用 container="Auto"，實際輸出是 .webm，不是 .mp4。
# 任何「找出成品影片」的判斷都必須認這一組副檔名，不能寫死 .mp4。
VIDEO_EXT = (".mp4", ".webm", ".mkv", ".mov")
MEDIA_EXT = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
             ".mkv": "video/x-matroska", ".png": "image/png", ".jpg": "image/jpeg",
             ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
             ".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac",
             ".m4a": "audio/mp4"}
# 主控台被 QuickEdit 暫停時（在視窗裡點一下或選到文字），往它寫東西
# 會永遠卡住。而每一個 /api/ 請求都會寫一行 log，於是整個 API 停擺、
# 靜態檔卻照常送——看起來就是「網頁活著但什麼都不能按」。
# 改成投進有上限的佇列，背景執行緒去寫；主控台卡住就丟幾行 log，
# 請求永遠不會被擋住。
_LOGQ = queue.Queue(maxsize=4000)
_LOGDROP = [0]


def slog(line):
    """絕不阻塞的 log。佇列滿了就丟，寧可掉訊息也不能掉服務。"""
    if not line.endswith(chr(10)):
        line += chr(10)
    try:
        _LOGQ.put_nowait(line)
    except queue.Full:
        _LOGDROP[0] += 1


def _log_worker():
    while True:
        line = _LOGQ.get()
        try:
            n = _LOGDROP[0]
            if n:
                _LOGDROP[0] = 0
                line = "  [log] 主控台停住期間丟掉 %d 行" % n + chr(10) + line
            sys.stderr.write(line)
            sys.stderr.flush()
        except Exception:
            pass


threading.Thread(target=_log_worker, daemon=True).start()
LOCK = threading.Lock()
# QLOCK 保護記憶體內的佇列快取／進度／預覽；LOCK 仍只保護磁碟索引的讀改寫。
# 規則：需要兩者時一律先 QLOCK 再 LOCK；兩者都不得在呼叫 ComfyUI 期間持有。
QLOCK = threading.RLock()
QROWS = {}   # jid -> row（磁碟的記憶體鏡像）
QLIVE = {}   # jid -> {progress, stage, prev_step}（僅記憶體，重啟即失）
QPREV = {}   # jid -> 預覽影像 bytes（僅記憶體）
# 正在算的那一支的進度與預覽（用 QLOCK 保護）。QLIVE/QPREV 是用任務清單的列 id 當 key，
# 不在清單裡的工作（I2VA Continue 的章節）對不上，所以另外依 prompt_id 記一份。
# ComfyUI 一次只算一支，留一格就夠。
LIVE_ONE = {"pid": "", "mime": "", "raw": b"", "tick": 0, "step": "", "progress": None}
COMFY_LAST_STATE = {}   # prompt_id -> last state string, so the console logs transitions, not every poll


def comfy_note(pid, state, extra=""):
    """Print one line when a ComfyUI job changes state (queued -> running -> done/error)."""
    key = state + ("|" + extra if extra else "")
    if COMFY_LAST_STATE.get(pid) != key:
        COMFY_LAST_STATE[pid] = key
        slog("  comfy %s : %s%s" % (pid[:8], state, (" " + extra) if extra else ""))
        if state in ("done", "error"):
            COMFY_LAST_STATE.pop(pid, None)
MAX_BODY = 24 * 1024 * 1024
MV_MAX_IMAGES = 24            # FL2VA Movie 分鏡張數上限；N 張圖 = N-1 個片段
IC_MAX_CHAPTERS = 24          # I2VA Continue 章數上限（前端同一個數字）
ID_RE = re.compile(r"^[0-9a-f]{8,32}$")


def ensure():
    os.makedirs(HIST, exist_ok=True)
    os.makedirs(PROMPTS, exist_ok=True)
    os.makedirs(LORAS, exist_ok=True)
    os.makedirs(UPLOADS, exist_ok=True)
    os.makedirs(QUEUE, exist_ok=True)
    if not os.path.exists(UINDEX):
        save_uindex([])
    if not os.path.exists(INDEX):
        save_index([])
    if not os.path.exists(PINDEX):
        save_pindex([])
    if not os.path.exists(LINDEX):
        save_lindex([])


def load_pindex():
    try:
        with open(PINDEX, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_pindex(rows):
    tmp = PINDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    os.replace(tmp, PINDEX)


def load_lindex():
    try:
        with open(LINDEX, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_lindex(rows):
    tmp = LINDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    os.replace(tmp, LINDEX)


def norm_lora(body):
    """Validate/normalise a LoRA preset body -> (rec, err)."""
    name = (str(body.get("name", "")).strip() or "未命名")[:80]
    main = body.get("main", "")
    if isinstance(main, list):
        main = ", ".join(str(x).strip() for x in main if str(x).strip())
    main = str(main).strip()[:200]
    subs_in = body.get("subs", [])
    subs, seen = [], set()
    if isinstance(subs_in, str):
        subs_in = [x.strip() for x in re.split(r"[,，]", subs_in) if x.strip()]
    for it in subs_in or []:
        alias = ""
        if isinstance(it, dict):
            key, gloss = str(it.get("key", "")).strip(), str(it.get("gloss", "")).strip()
            alias = str(it.get("alias", "") or "").strip()
        else:
            parts = str(it).split("=", 1)
            key, rest = parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")
            if "|" in rest:
                gloss, alias = rest.split("|", 1)
                gloss, alias = gloss.strip(), alias.strip()
            else:
                gloss = rest
        if not key or key.lower() in seen:
            continue
        if not re.match(r"^[A-Za-z0-9_\-]{1,32}$", key):
            return None, "bad sub key %r (letters/digits/_/- only, max 32)" % key
        seen.add(key.lower())
        subs.append({"key": key, "gloss": gloss[:120], "alias": alias[:40]})
    return {"name": name, "main": main, "subs": subs}, None


def load_index():
    try:
        with open(INDEX, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_index(rows):
    tmp = INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    os.replace(tmp, INDEX)


# ---------------------------------------------------------------- uploads store
# One entry per distinct image CONTENT (sha1 of the full-size bytes). Several history records - and
# several uploads of the same file - all point at the same entry. History records keep their own
# copy of the image too, so deleting a record never touches the upload; deleting an upload cascades
# to every record that references it.
HASH_RE = re.compile(r"^[0-9a-f]{40}$")


def load_uindex():
    try:
        with open(UINDEX, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_uindex(rows):
    tmp = UINDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    os.replace(tmp, UINDEX)


def _img_dims(blob):
    """(w, h) from JPEG/PNG headers without PIL; (0, 0) if unknown."""
    try:
        if blob[:8] == b"\x89PNG\r\n\x1a\n":
            return int.from_bytes(blob[16:20], "big"), int.from_bytes(blob[20:24], "big")
        if blob[:2] == b"\xff\xd8":
            i = 2
            while i < len(blob) - 9:
                if blob[i] != 0xFF:
                    i += 1; continue
                m = blob[i + 1]
                if m in (0xC0, 0xC1, 0xC2):
                    return int.from_bytes(blob[i + 7:i + 9], "big"), int.from_bytes(blob[i + 5:i + 7], "big")
                i += 2 + int.from_bytes(blob[i + 2:i + 4], "big")
    except Exception:
        pass
    return 0, 0


ORIG_EXTS = ("jpg", "png", "webp")


def orig_path(folder, stem):
    """Full-resolution original next to a 1024px working copy: <stem>.orig.<ext>. Returns (path, ext) or (None, "")."""
    for ext in ORIG_EXTS:
        fp = os.path.join(folder, stem + ".orig." + ext)
        if os.path.exists(fp):
            return fp, ext
    return None, ""


UPLOAD_HASH_RE = re.compile(r"^[0-9a-f]{40}$")


def upload_blob(h):
    """依雜湊從上傳庫取圖。優先全解析度原圖，沒有才取 1024px 工作副本
    （沒有 .orig 就表示當初上傳的圖本來就 ≤ 1024px，工作副本即原圖）。
    回傳 (bytes, ext)；找不到回傳 (None, "jpg")。"""
    if not isinstance(h, str) or not UPLOAD_HASH_RE.match(h):
        return None, "jpg"
    op, oext = orig_path(UPLOADS, h)
    if op:
        return open(op, "rb").read(), oext
    fp = os.path.join(UPLOADS, h + ".jpg")
    if os.path.exists(fp):
        return open(fp, "rb").read(), "jpg"
    return None, "jpg"


def parse_data_image(data):
    """'data:image/png;base64,...' -> (bytes, ext) ; (None, '') if not an image data URL."""
    if not isinstance(data, str) or not data.startswith("data:image"):
        return None, ""
    try:
        head, b64 = data.split(",", 1)
        blob = base64.b64decode(b64)
    except Exception:
        return None, ""
    mime = head[5:].split(";", 1)[0].lower()
    ext = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/webp": "webp"}.get(mime, "jpg")
    return blob, ext


AUDIO_EXT_BY_MIME = {
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav", "audio/x-wav": "wav",
    "audio/wave": "wav", "audio/ogg": "ogg", "audio/opus": "opus", "audio/flac": "flac",
    "audio/x-flac": "flac", "audio/aac": "aac", "audio/mp4": "m4a", "audio/x-m4a": "m4a",
    "audio/webm": "webm",
}
MAX_REF_AUDIO = 3          # REF2VA 最多 3 個音訊參考（節點的 validate_reference_limits）
MAX_REF_FILES = 12         # 圖＋影片＋音訊合計上限


def hist_purge(rid):
    """刪掉一筆歷史紀錄的所有檔案。
    原本是固定副檔名清單，漏掉了附圖 .x{n}.jpg（REF2VA 最多 8 個）與音訊 .a{n}.*，
    每刪一筆就留下一堆孤兒。rid 已經過 ID_RE 檢查，用 glob 掃乾淨。"""
    import glob as _g
    for fp in _g.glob(os.path.join(HIST, rid + ".*")):
        try:
            os.remove(fp)
        except OSError:
            pass


def parse_data_audio(data):
    """'data:audio/mpeg;base64,...' -> (bytes, ext)；不是音訊就回 (None, '')。
    副檔名要給對：Director 的 load_audio 交給 PyAV 開檔，容器判斷吃得到副檔名。"""
    if not isinstance(data, str) or not data.startswith("data:audio"):
        return None, ""
    try:
        head, b64 = data.split(",", 1)
        blob = base64.b64decode(b64)
    except Exception:
        return None, ""
    mime = head[5:].split(";", 1)[0].lower()
    return blob, AUDIO_EXT_BY_MIME.get(mime, "mp3")


def upload_register(full_bytes, thumb_bytes, name, orig_bytes=b"", orig_ext=""):
    """Store an image in the uploads library (idempotent by content hash of the 1024px copy). Returns the hash.
    orig_bytes = the full-resolution original (what ComfyUI should receive); stored once per hash."""
    h = hashlib.sha1(full_bytes).hexdigest()
    fp = os.path.join(UPLOADS, h + ".jpg")
    with LOCK:
        rows = load_uindex()
        hit = next((r for r in rows if r.get("id") == h), None)
        if not os.path.exists(fp):
            with open(fp, "wb") as f:
                f.write(full_bytes)
        tp = os.path.join(UPLOADS, h + ".thumb.jpg")
        if thumb_bytes and not os.path.exists(tp):
            with open(tp, "wb") as f:
                f.write(thumb_bytes)
        oext = ""
        if orig_bytes and orig_ext in ORIG_EXTS:
            op = os.path.join(UPLOADS, h + ".orig." + orig_ext)
            if not os.path.exists(op) and orig_path(UPLOADS, h)[0] is None:
                with open(op, "wb") as f:
                    f.write(orig_bytes)
            oext = orig_path(UPLOADS, h)[1]
        if hit is None:
            w, hgt = _img_dims(full_bytes)
            row = {"id": h, "name": (name or "")[:200], "w": w, "h": hgt,
                   "size": len(full_bytes), "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "thumb": os.path.exists(tp)}
            if oext:
                ow, oh = _img_dims(orig_bytes) if orig_bytes else (0, 0)
                row.update({"orig_ext": oext, "ow": ow, "oh": oh, "osize": len(orig_bytes)})
            rows.insert(0, row)
            save_uindex(rows)
        else:
            changed = False
            # keep the first-seen name, but remember any others for search
            if name and name not in (hit.get("names") or []) and name != hit.get("name"):
                hit.setdefault("names", []).append(name[:200]); changed = True
            # an original arriving later for an image we only had at 1024px
            if oext and not hit.get("orig_ext"):
                ow, oh = _img_dims(orig_bytes) if orig_bytes else (0, 0)
                hit.update({"orig_ext": oext, "ow": ow, "oh": oh, "osize": len(orig_bytes)}); changed = True
            if changed:
                save_uindex(rows)
    return h


def upload_backfill():
    """One-time (idempotent) scan: register every history record's .full.jpg as an upload and link it.
    Makes imported / pre-feature history show up in the upload library."""
    rows = load_index()
    changed = 0
    for row in rows:
        rid = row.get("id")
        if not rid or not ID_RE.match(rid):
            continue
        rp = os.path.join(HIST, rid + ".json")
        fp = os.path.join(HIST, rid + ".full.jpg")
        if not os.path.exists(rp) or not os.path.exists(fp):
            continue
        try:
            with open(rp, encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:
            continue
        if rec.get("upload_id"):
            continue
        try:
            with open(fp, "rb") as f:
                full = f.read()
            tp = os.path.join(HIST, rid + ".jpg")
            thumb = open(tp, "rb").read() if os.path.exists(tp) else b""
        except Exception:
            continue
        h = upload_register(full, thumb, rec.get("image", ""))
        rec["upload_id"] = h
        try:
            with open(rp, "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=1)
            row["upload_id"] = h
            changed += 1
        except Exception:
            pass
    if changed:
        with LOCK:
            save_index(rows)
    return changed


def upload_history_ids(h):
    """History record ids that reference this upload."""
    return [r["id"] for r in load_index() if r.get("upload_id") == h]


def media_path(rel):
    """把相對路徑安全地解析到 MEDIA_ROOT 內，擋掉任何逃逸。"""
    rel = rel.replace("\\", "/").strip("/")
    if not rel or rel.startswith("..") or "/../" in rel or rel.endswith("/.."):
        return None
    fp = os.path.realpath(os.path.join(MEDIA_ROOT, rel))
    root = os.path.realpath(MEDIA_ROOT)
    if not (fp == root or fp.startswith(root + os.sep)):
        return None
    if os.path.splitext(fp)[1].lower() not in MEDIA_EXT:
        return None
    return fp


# ── 遠端 ComfyUI 的成品鏡像 ────────────────────────────────────────────
# ComfyUI 在別台機器時，它的 output 資料夾不在這台的磁碟上。可是審查抽幀、
# 長片合併、縮圖、任務清單播放全都是讀 media_root 裡的本機檔案——以前
# 同一台時理所當然，分開之後每一條都會變成「影片不存在」。
# 缺檔時跟 ComfyUI 的 /view 要一份，放進 media_root 的同一個相對路徑，
# 之後就是普通的本機檔案。ComfyUI 在本機且 media_root 指對時檔案本來就在，
# 這裡一行都不會跑。
MEDIA_FETCH_LOCK = threading.Lock()
_media_fetching = {}     # 本機路徑 -> Event：同一個檔同時只抓一次，其他人等它
_media_fetch_fail = {}   # 本機路徑 -> 失敗時間：一頁上百張封面別對同一個 404 反覆敲


def _comfy_view_url(rel, extra=""):
    rel = rel.replace("\\", "/").strip("/")
    sub, name = rel.rsplit("/", 1) if "/" in rel else ("", rel)
    return "%s/view?filename=%s&subfolder=%s&type=output%s" % (
        COMFY_URL, quote(name), quote(sub), extra)


def _comfy_view_read(url, out, budget):
    """把 /view 串流寫進 out。urlopen 的 timeout 是單次 socket 操作的上限，
    不是整份檔的——慢慢滴的連線可以永遠不觸發它，所以另外算牆鐘。"""
    t_end = time.time() + budget
    with urllib.request.urlopen(url, timeout=min(30, budget)) as r, open(out, "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
            if time.time() > t_end:
                raise TimeoutError("超過 %d 秒還沒下載完" % budget)


def media_fetch(rel, budget=180):
    """確保 rel 在 media_root 裡有一份；回傳本機路徑，拿不到回 None。"""
    fp = media_path(rel)
    if not fp:
        return None
    if os.path.isfile(fp):
        return fp
    if time.time() - _media_fetch_fail.get(fp, 0) < 60:
        return None
    with MEDIA_FETCH_LOCK:
        ev = _media_fetching.get(fp)
        mine = ev is None
        if mine:
            ev = _media_fetching[fp] = threading.Event()
    if not mine:
        ev.wait(budget)
        return fp if os.path.isfile(fp) else None
    tmp = fp + ".part"
    try:
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        t0 = time.time()
        _comfy_view_read(_comfy_view_url(rel), tmp, budget)
        os.replace(tmp, fp)
        slog("  [media] 從 ComfyUI 取回 %s（%.1f MB，%.1fs）" % (
            rel, os.path.getsize(fp) / 1048576.0, time.time() - t0))
        return fp
    except Exception as e:
        _media_fetch_fail[fp] = time.time()
        slog("  [media] 從 ComfyUI 取回 %s 失敗：%s" % (rel, str(e)[:160]))
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None
    finally:
        with MEDIA_FETCH_LOCK:
            _media_fetching.pop(fp, None)
        ev.set()


def media_prefetch(*rels):
    """任務一完成就在背景把成品拉回來，審查與長片的下一段不用再等下載。"""
    rels = [r for r in rels if r]
    if rels:
        threading.Thread(target=lambda: [media_fetch(r) for r in rels], daemon=True).start()


# ── 成品縮圖快取 ────────────────────────────────────────────────────────
# 首幀 PNG 是全解析度的（實測平均 2.6 MB，931 個檔案共 2.4 GB）。
# 任務清單一頁上百張卡片，直接送原檔等於要傳幾百 MB，捲動時就一片黑。
# 這裡用 ffmpeg 壓成 320px 寬的 JPEG 存起來，之後直接命中快取。
MEDIA_THUMBS = os.path.join(ROOT, "thumbs")
THUMB_W = 320
THUMB_LOCK = threading.RLock()
_thumb_busy = {}


def media_thumb(rel):
    """回傳縮圖檔路徑；沒有就用 ffmpeg 生一張。來源優先用首幀 PNG，
    沒有首幀就直接從影片第一格抓。失敗回 None，呼叫端退回原檔。"""
    src = media_path(rel)
    if not src or not os.path.isfile(src):
        # 傳進來的是影片路徑時，先試它的首幀 PNG
        cand = os.path.splitext(rel)[0] + "-first-frame.png"
        src = media_path(cand)
        if not (src and os.path.isfile(src)):
            # 本機沒有：多半是 ComfyUI 在別台，交給遠端那條路
            return media_thumb_remote(rel)
    try:
        st = os.stat(src)
        key = hashlib.sha1(("%s|%d|%d|%d" % (src, st.st_size, int(st.st_mtime), THUMB_W))
                           .encode("utf-8")).hexdigest()
    except OSError:
        return None
    out = os.path.join(MEDIA_THUMBS, key + ".jpg")
    if os.path.exists(out):
        return out
    with THUMB_LOCK:
        if os.path.exists(out):
            return out
        if _thumb_busy.get(key):
            return None                     # 另一個請求正在生，這次先退回原檔
        _thumb_busy[key] = True
    try:
        ff = find_ffmpeg()
        if not ff:
            return None
        os.makedirs(MEDIA_THUMBS, exist_ok=True)
        tmp = out + ".tmp.jpg"
        r = subprocess.run([ff, "-v", "error", "-y", "-i", src,
                            "-frames:v", "1", "-vf", "scale=%d:-2" % THUMB_W,
                            "-q:v", "5", tmp],
                           # 一張 320px 的抽幀要 60 秒的話早就失敗了。而且 <img src>
                           # 沒辦法在前端設期限，這裡是唯一能擋的地方。
                           capture_output=True, timeout=15)
        if r.returncode != 0 or not os.path.exists(tmp):
            return None
        os.replace(tmp, out)
        return out
    except Exception:
        return None
    finally:
        with THUMB_LOCK:
            _thumb_busy.pop(key, None)


def media_thumb_remote(rel):
    """ComfyUI 在別台時的封面。首幀 PNG 是全解析度的（平均 2.6 MB），
    一頁上百張卡片全搬回來就是幾百 MB 走外網。/view 的 preview 參數會在
    ComfyUI 那邊先轉成 JPEG（實測約小十倍），拿它來縮就好；
    完整的 PNG 等真的有人要看（或長片要拿尾幀）時才由 media_fetch 搬。

    影片不為了一張 320px 的封面整支搬回來：先找它旁邊的圖（v18 的
    -first-frame.png、VHS 的同名 .png），用那張的預覽。實測 1529 支影片裡
    1136 支有首幀、6 支有同名圖；剩下 387 支一張都沒有，才讓 ffmpeg 直接讀
    遠端的 /view——它支援 Range，ffmpeg 只抓得到第一格所需的那一小段。"""
    fp = media_path(rel)
    if not fp:
        return None
    src_rel, from_video = rel, False
    if os.path.splitext(rel)[1].lower() not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        base = os.path.splitext(rel)[0]
        cands = [base + "-first-frame.png", base + ".png", re.sub(r"[-_]audio$", "", base) + ".png"]
        known = ASSETS["rows"]
        if known:
            src_rel = next((c for c in cands if c in known), None)
            from_video = src_rel is None
        else:
            src_rel = cands[0]          # 沒有清單可查：照舊猜首幀
    key = hashlib.sha1(("remote|%s|%d" % (rel.replace("\\", "/").strip("/"), THUMB_W))
                       .encode("utf-8")).hexdigest()
    out = os.path.join(MEDIA_THUMBS, key + ".jpg")
    if os.path.exists(out):
        return out
    if time.time() - _media_fetch_fail.get("thumb|" + fp, 0) < 60:
        return None
    ff = find_ffmpeg()
    if not ff:
        return None
    with THUMB_LOCK:
        if os.path.exists(out):
            return out
        if _thumb_busy.get(key):
            return None
        _thumb_busy[key] = True
    tmp_src = out + ".src.jpg"
    try:
        os.makedirs(MEDIA_THUMBS, exist_ok=True)
        # 縮圖端點沒有前端期限可言（<img src> 接不上 AbortController），
        # 這裡的上限就是全部。
        tmp = out + ".tmp.jpg"
        if from_video:
            # -rw_timeout（微秒）是單次讀寫的上限；整段再用 subprocess 的 timeout 封頂
            inp, budget = _comfy_view_url(rel), 30
            extra = ["-rw_timeout", "15000000"]
        else:
            _comfy_view_read(_comfy_view_url(src_rel, "&preview=jpeg;85"), tmp_src, 20)
            inp, budget, extra = tmp_src, 15, []
        r = subprocess.run([ff, "-v", "error", "-y"] + extra + ["-i", inp,
                            "-frames:v", "1", "-vf", "scale=%d:-2" % THUMB_W,
                            "-q:v", "5", tmp], capture_output=True, timeout=budget)
        if r.returncode != 0 or not os.path.exists(tmp):
            return None
        os.replace(tmp, out)
        return out
    except Exception as e:
        _media_fetch_fail["thumb|" + fp] = time.time()
        slog("  [media] 遠端縮圖 %s 失敗：%s" % (rel, str(e)[:120]))
        return None
    finally:
        try:
            os.unlink(tmp_src)
        except OSError:
            pass
        with THUMB_LOCK:
            _thumb_busy.pop(key, None)


# ── ComfyUI 那台的檔案清單 ────────────────────────────────────────────
# 鏡像只有「被拉回來過」的檔案。要看 ComfyUI 那邊的全部，只有 /api/assets
# 列得出來——它要 ComfyUI 啟動時加 --enable-assets。實測其他兩條都不行：
# /internal/files/output 用 os.scandir 不遞迴，3975 個檔只看得到根目錄的 177 個；
# /history 只記得這次啟動後跑過的工作。assets 分頁拉完 3973 筆約 2.6 秒
# （差的 2 筆是 0 byte 空檔，它本來就跳過）。
# DELETE /api/assets/{id} 只刪它自己資料庫的紀錄、不碰磁碟——這裡也不會去呼叫。
ASSETS = {"t": 0.0, "rows": None, "off_until": 0.0, "err": ""}
ASSETS_LOCK = threading.Lock()      # 保護 ASSETS
ASSETS_FETCH = threading.Lock()     # 同一時間只拉一份，其他人等它的結果
ASSETS_TTL = 60
# created_at 是被索引的時間不是檔案時間（第一次掃描時全部同一秒），沒有可用的
# 檔案時間。成品的路徑本身就帶著：video/2026-09-22/004740_00001_audio.webm。
_PATH_TIME = re.compile(r"(?:^|/)(\d{4})-(\d{2})-(\d{2})/(\d{2})(\d{2})(\d{2})_")
# 路徑裡沒有時間的（根目錄那些）跟 /view 要 Last-Modified。一個檔一次，存檔，
# 檔名是 ComfyUI 的流水號、不會被覆寫，所以不用失效。
REMOTE_MTIME_PATH = os.path.join(ROOT, "thumbs", "remote-mtime.json")
REMOTE_MTIME = {}
REMOTE_MTIME_LOCK = threading.Lock()
_remote_mtime_busy = [False]


def _path_time(p):
    m = _PATH_TIME.search(p)
    if not m:
        return 0
    y, mo, d, h, mi, s = (int(x) for x in m.groups())
    if not (1 <= mo <= 12 and 1 <= d <= 31 and h < 24 and mi < 60 and s < 60):
        return 0
    try:
        return int(time.mktime((y, mo, d, h, mi, s, 0, 0, -1)))   # ComfyUI 那台的本地時間
    except (OverflowError, ValueError):
        return 0


def _remote_mtime_load():
    if REMOTE_MTIME:
        return
    try:
        with open(REMOTE_MTIME_PATH, encoding="utf-8") as f:
            REMOTE_MTIME.update(json.load(f))
    except Exception:
        pass


def _remote_mtime_fill(paths, wait_s):
    """同時開 8 條去問 Last-Modified。第一次約 190 個檔，一條條問要快一分鐘；
    這個請求最多等 wait_s 秒，剩下的在背景問完、下一次清單就有。"""
    import concurrent.futures as cf
    from email.utils import parsedate_to_datetime

    def one(p):
        req = urllib.request.Request(_comfy_view_url(p), method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as r:
            lm = r.headers.get("Last-Modified")
        if lm:
            with REMOTE_MTIME_LOCK:
                REMOTE_MTIME[p] = int(parsedate_to_datetime(lm).timestamp())

    def save():
        try:
            os.makedirs(os.path.dirname(REMOTE_MTIME_PATH), exist_ok=True)
            with REMOTE_MTIME_LOCK:
                data = json.dumps(REMOTE_MTIME)
            tmp = REMOTE_MTIME_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, REMOTE_MTIME_PATH)
        except Exception:
            pass

    if _remote_mtime_busy[0] or not paths:
        return
    _remote_mtime_busy[0] = True
    ex = cf.ThreadPoolExecutor(max_workers=8)
    futs = [ex.submit(one, p) for p in paths]
    cf.wait(futs, timeout=wait_s)

    def finish():
        cf.wait(futs)
        ex.shutdown(wait=False)
        save()
        _remote_mtime_busy[0] = False
    threading.Thread(target=finish, daemon=True).start()


def comfy_assets_all(tags_all, budget=20):
    """把 /api/assets 某個標籤底下的紀錄全部分頁拉完。沒開 --enable-assets 會丟 404。"""
    out, after, t_end = [], None, time.time() + budget
    while True:
        q = {"tags_all": tags_all, "limit": 500, "sort": "name", "order": "asc"}
        if after:
            q["after"] = after
        j = comfy_api("/api/assets?" + urlencode(q), timeout=15)
        out.extend(j.get("assets") or [])
        after = j.get("next_cursor")
        if not j.get("has_more") or not after:
            return out
        if time.time() > t_end:
            raise TimeoutError("清單超過 %d 秒還沒拉完" % budget)


def comfy_assets(fresh=False):
    """ComfyUI 那台 output 的完整清單：{相對路徑: {size, mtime}}。
    沒開 --enable-assets 或連不上時回 None（拉過的話回上一份）。"""
    now = time.time()
    with ASSETS_LOCK:
        if not fresh and ASSETS["rows"] is not None and now - ASSETS["t"] < ASSETS_TTL:
            return ASSETS["rows"]
        if now < ASSETS["off_until"]:
            return ASSETS["rows"]
    if not ASSETS_FETCH.acquire(timeout=25):
        return ASSETS["rows"]
    try:
        with ASSETS_LOCK:          # 等鎖的時候別人可能已經拉好了
            if ASSETS["t"] > now:
                return ASSETS["rows"]
        rows = {}
        for a in comfy_assets_all("output"):
            p = str(a.get("loader_path") or "").replace("\\", "/").strip("/")
            if p and os.path.splitext(p)[1].lower() in MEDIA_EXT and ".." not in p.split("/"):
                rows[p] = {"size": int(a.get("size") or 0), "mtime": _path_time(p)}
        _remote_mtime_load()
        missing = []
        for p, r in rows.items():
            if not r["mtime"]:
                r["mtime"] = REMOTE_MTIME.get(p, 0)
                if not r["mtime"]:
                    missing.append(p)
        if missing:
            _remote_mtime_fill(missing, 10)
            for p in missing:
                rows[p]["mtime"] = REMOTE_MTIME.get(p, 0)
        with ASSETS_LOCK:
            ASSETS.update(t=time.time(), rows=rows, err="", off_until=0.0)
        return rows
    except Exception as e:
        # 404＝ComfyUI 沒開 --enable-assets：五分鐘內別再問。其他錯誤三十秒後再試。
        msg = str(e)
        with ASSETS_LOCK:
            ASSETS["off_until"] = time.time() + (300 if "404" in msg else 30)
            ASSETS["err"] = ("ComfyUI 沒有開 --enable-assets" if "404" in msg else msg[:160])
            return ASSETS["rows"]
    finally:
        ASSETS_FETCH.release()


# ── LoRA：清單與送單時的替換 ───────────────────────────────────────────
# 工作流裡載 LoRA 的是 DaSiWa_LTX2LoraLoader，內容整串放在 stack_data（JSON 字串）。
# 後端逐筆套用、沒有格數上限——UI 上的 7 格只是面板預設，有 +/- 可增減——所以
# 「必須 5 個＋配方 10 個」最多 15 筆直接寫進去就行。找不到的檔案節點只會記一行
# log 然後略過，影片照樣算出來，所以送單前要自己先確認檔案存在。
LORA_NODE = "DaSiWa_LTX2LoraLoader"
LORA_CACHE = {"t": 0.0, "rows": None}
LORA_LOCK = threading.Lock()


def comfy_loras(fresh=False):
    """ComfyUI 那台的 LoRA 清單：[{name, size, trained_words, base_model}]。

    名字一律用 /models/loras 的寫法：那就是 LoraLoader 下拉選單的值，ComfyUI 在
    Windows 上是反斜線（MiniMax H3\\x.safetensors），寫進 stack_data 必須一字不差。
    /api/assets 回的是正斜線，只拿來補大小、觸發詞（trained_words，讀自檔案內嵌
    的訓練資料，沒寫的就沒有）與底模；沒開 --enable-assets 就退回 experiment 那條補大小。"""
    with LORA_LOCK:
        if not fresh and LORA_CACHE["rows"] is not None and time.time() - LORA_CACHE["t"] < 60:
            return LORA_CACHE["rows"]
    names = comfy_api("/models/loras", timeout=15)
    if not isinstance(names, list):
        raise ValueError("ComfyUI 的 /models/loras 回應不是清單")
    meta = {}
    try:
        for a in comfy_assets_all("model_type:loras", budget=15):
            md = a.get("metadata") or {}
            tw = md.get("trained_words")
            meta[str(a.get("loader_path") or "").replace("\\", "/")] = {
                "size": int(a.get("size") or 0),
                "trained_words": [str(x) for x in tw][:12] if isinstance(tw, list) else [],
                "base_model": str(md.get("base_model") or "")[:60]}
    except Exception:
        try:
            for x in comfy_api("/experiment/models/loras", timeout=15) or []:
                meta[str(x.get("name") or "").replace("\\", "/")] = {"size": int(x.get("size") or 0)}
        except Exception:
            pass
    rows = []
    for n in names:
        n = str(n)
        m = meta.get(n.replace("\\", "/"), {})
        rows.append({"name": n, "size": m.get("size", 0),
                     "trained_words": m.get("trained_words", []), "base_model": m.get("base_model", "")})
    with LORA_LOCK:
        LORA_CACHE.update(t=time.time(), rows=rows)
    return rows


def apply_lora_stack(g, wf):
    """把 wf["loras"]（前端算好的「必須＋配方」）寫進 LoRA 節點的 stack_data。

    wf 沒帶 loras＝使用者兩邊都沒設定，模板原封不動——舊的用法不受影響。
    回傳 {舊字串: 新字串}，交給 UI 工作流同步，拖回 ComfyUI 看到的才是這支實際用的 LoRA。"""
    loras = (wf or {}).get("loras")
    if not isinstance(loras, list):
        return {}
    rows, seen = [], set()
    for x in loras[:30]:
        if not isinstance(x, dict):
            continue
        name = str(x.get("lora") or "").strip()
        if not name or name == "None" or name in seen:
            continue
        try:
            st = float(x.get("str", 1.0))
        except (TypeError, ValueError):
            st = 1.0
        seen.add(name)
        # vs／as 是 LTX-2.3 模式的影像／音訊倍率；Basic 模式實際強度＝str×vs。都給 1，
        # 權重只放在 str，兩種模式下意思都一樣。
        rows.append({"on": True, "lora": name, "str": round(max(-10.0, min(10.0, st)), 4),
                     "vs": 1, "as": 1})
    targets = [n for n in g.values() if n.get("class_type") == LORA_NODE]
    if not targets:
        raise SubmitError("這個工作流沒有 %s 節點，LoRA（必須／配方）套不上去。"
                          "換一個有 LoRA 載入節點的工作流，或把 LoRA 設定清空。" % LORA_NODE, 400)
    if rows:
        have = {r["name"] for r in comfy_loras()}
        missing = [r["lora"] for r in rows if r["lora"] not in have]
        if missing:
            have = {r["name"] for r in comfy_loras(fresh=True)}   # 可能是剛放進去的，快取還沒更新
            missing = [r["lora"] for r in rows if r["lora"] not in have]
        if missing:
            raise SubmitError("ComfyUI 上找不到這些 LoRA：%s——檔案被移走或改名了？"
                              "請到 ComfyUI 設定的 LoRA 面板更新。" % "、".join(missing), 400)
    # 前端是 JSON.stringify 的格式（沒有空白），寫成一樣的，面板讀回去不會被當成改過
    new = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    swaps = {}
    for node in targets:
        ins = node.setdefault("inputs", {})
        old = ins.get("stack_data")
        ins["stack_data"] = new
        if isinstance(old, str) and old != new:
            swaps[old] = new
    return swaps


_LORA_SECS = ("integrated_multimodal_description", "overall_soundscape", "non_diegetic_music",
              "subject_definitions", "summary", "retention_analysis", "detailed_description")
# 分鏡標記，後續鏡頭的官方寫法是 "[Shot 2] At 00:03.000, "——插在時間戳之後，不拆開它
_SHOT_HEAD = re.compile(r"\[Shot\s*\d+\]\s*(At\s+\d{1,2}:\d{2}(?:\.\d+)?\s*,\s*)?", re.I)


def lora_prompt_apply(text, mode, act):
    """把 LoRA 的觸發詞（前端把必須＋配方合成一行）插進主段落「每一段的開頭」。

    不交給寫 prompt 的 LLM：它會把觸發詞當劇情改寫、翻譯、搬位置，前端的清理
    還會把排在第一行之前的字整段刪掉。DaSiWa 0.4.35 的 Director 則是把
    external_prompt_overwrite 原樣交給 Qwen tokenizer——不截斷、不改大小寫、
    不解析權重——所以 LLM 之後、送出之前插進去，就保證一字不差到模型。

    位置：主段落（base 是 integrated_multimodal_description，REF2VA 是
    detailed_description）裡每個 [Shot N] 之後。不放在結尾：Qwen 那段是 causal，
    排在後面的字影響不到前面描述的理解。主段落沒有分鏡標記就放在本文開頭；
    連主段落標頭都沒有（T2VA「直接使用」送的是中文劇本）就放在整段最前面。
    那一段已經以觸發詞開頭的（例如手動寫過）不重複插。回傳 (新文字, 插了幾處)。"""
    act = " ".join(str(act or "").split())[:1200]      # 必須、配方各 500 字，合起來不能在這裡被截掉
    text = str(text or "")
    if not act or not text.strip():
        return text, 0
    piece = act + ("" if act[-1] in ",.;:!?，。；：！？" else ",") + " "
    main = "detailed_description" if str(mode or "").lower() == "ref2va" else "integrated_multimodal_description"
    m = (re.search(r"(?mi)^[ \t]*" + main + r"[ \t]*:[ \t]*", text)
         or re.search(main + r"[ \t]*:?[ \t]*", text, re.I))
    if not m:
        if text.lstrip().startswith(act):
            return text, 0
        return act + "\n\n" + text, 1
    start = m.end()
    others = "|".join(n for n in _LORA_SECS if n != main)
    e = re.compile(r"(?mi)^[ \t]*(?:" + others + r")[ \t]*:").search(text, start)
    end = e.start() if e else len(text)
    body = text[start:end]
    spots = []
    for sm in _SHOT_HEAD.finditer(body):
        before = body[:sm.start()].rstrip(" \t")
        # 帶「At MM:SS.mmm,」切點時間的一定是段落開頭。官方格式常把整段寫成一行，
        # 前一個鏡頭以對白收尾：「…</d> [Shot 2] At 00:05.000, …」——只看前一個字的話，
        # 「>」不是句號，第 2 個鏡頭起就全被當成句中引用而漏插（歷史紀錄裡實際有這種）。
        # 沒有時間戳的才可能是句中提到（「延續 [Shot 1] 的構圖」），那種才看前一個字。
        if not sm.group(1) and before and before[-1] not in ".!?。！？:\n)]>\"'”’」』":
            continue
        spots.append(sm.end())
    if not spots:
        spots = [len(body) - len(body.lstrip())]
    n = 0
    for pos in reversed(spots):
        if body[pos:].startswith(act):
            continue
        body = body[:pos] + piece + body[pos:]
        n += 1
    return text[:start] + body + text[end:], n


# ── 檢查更新 ─────────────────────────────────────────────────────────
# 問 GitHub 的 releases/latest（不含草稿與預覽版）。沒登入的 API 一小時只給 60 次，
# 所以結果留十分鐘；按「檢查更新」時才帶 fresh 重問。
UPDATE_CACHE = {"t": 0.0, "data": None}
UPDATE_LOCK = threading.Lock()


def _ver_tuple(v):
    """'v2.5.2' -> (2, 5, 2, 0)；'2.6' -> (2, 6, 0, 0)。只比數字，忽略前綴與後綴。"""
    nums = [int(x) for x in re.findall(r"\d+", str(v or ""))[:4]]
    return tuple(nums + [0] * (4 - len(nums)))


def check_update(fresh=False):
    with UPDATE_LOCK:
        age = time.time() - UPDATE_CACHE["t"]
        # 按鈕每次都帶 fresh；連按的話一分鐘內還是用快取，免得把每小時 60 次用光
        if UPDATE_CACHE["data"] and (age < 60 or (not fresh and age < 600)):
            return UPDATE_CACHE["data"]
    req = urllib.request.Request("https://api.github.com/repos/%s/releases/latest" % APP_REPO,
                                 headers={"User-Agent": "%s/%s" % (APP_NAME.replace(" ", "-"), APP_VERSION),
                                          "Accept": "application/vnd.github+json"})
    # 前端的預算是 30 秒；這裡要比它早放手（伺服器端的天花板一律低於前端）
    with urllib.request.urlopen(req, timeout=10) as r:
        j = json.loads(r.read().decode("utf-8"))
    tag = str(j.get("tag_name") or "")
    data = {"current": APP_VERSION, "latest": tag.lstrip("vV"), "tag": tag,
            "newer": _ver_tuple(tag) > _ver_tuple(APP_VERSION),
            "name": str(j.get("name") or tag)[:200],
            "url": str(j.get("html_url") or (APP_REPO_URL + "/releases/latest")),
            "published_at": str(j.get("published_at") or "")}
    with UPDATE_LOCK:
        UPDATE_CACHE.update(t=time.time(), data=data)
    return data


def media_list(limit=5000, remote=True, fresh=False):
    """本機 media_root（ComfyUI 在別台時就是鏡像）＋ ComfyUI 那台的清單，以路徑合併。
    local/remote 兩個旗標讓前端知道哪些能刪（只有這台有的副本能刪）。"""
    local = {}
    root = os.path.realpath(MEDIA_ROOT)
    if os.path.isdir(root):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                ext = os.path.splitext(name)[1].lower()
                if ext not in MEDIA_EXT:
                    continue
                fp = os.path.join(dirpath, name)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                mime = MEDIA_EXT[ext]
                p = os.path.relpath(fp, root).replace(os.sep, "/")
                local[p] = {"path": p, "name": name, "size": st.st_size,
                            "mtime": int(st.st_mtime), "kind": mime.split("/")[0],
                            "mime": mime, "local": True, "remote": False}
    rem = comfy_assets(fresh) if remote else None
    if rem is None and not os.path.isdir(root):
        return None
    rows = dict(local)
    for p, a in (rem or {}).items():
        if p in rows:
            # 鏡像檔的 mtime 是「拉回來的時間」，排序要用它真正產生的時間
            rows[p]["remote"] = True
            if a["mtime"]:
                rows[p]["mtime"] = a["mtime"]
            continue
        mime = MEDIA_EXT[os.path.splitext(p)[1].lower()]
        rows[p] = {"path": p, "name": p.rsplit("/", 1)[-1], "size": a["size"],
                   "mtime": a["mtime"], "kind": mime.split("/")[0], "mime": mime,
                   "local": False, "remote": True}
    out = sorted(rows.values(), key=lambda r: r["mtime"], reverse=True)
    total = len(out)
    return {"total": total, "truncated": total > limit, "files": out[:limit],
            "remote": {"enabled": rem is not None, "count": len(rem or {}),
                       "only": sum(1 for r in out if not r["local"]),
                       "error": ASSETS["err"] if remote else ""}}


def comfy_api(path, data=None, timeout=30):
    import urllib.request as _u
    maybe_reload_config()
    req = _u.Request(COMFY_URL + path,
                     data=json.dumps(data).encode() if data is not None else None,
                     headers={"Content-Type": "application/json"} if data is not None else {})
    r = json.loads(_u.urlopen(req, timeout=timeout).read() or b"{}")
    COMFY_SEEN[0] = time.time()
    return r


def llama_api(path, data=None, timeout=30):
    """代理 llama-server：位址只存在 config.json，瀏覽器一律走同源 /api/llama/*。"""
    import urllib.request as _u
    base = CONFIG["llama_url"].rstrip("/")
    req = _u.Request(base + path,
                     data=json.dumps(data).encode() if data is not None else None,
                     headers={"Content-Type": "application/json"} if data is not None else {})
    return json.loads(_u.urlopen(req, timeout=timeout).read() or b"{}")


# 一次 /api/comfy/run 最多傳 12 個檔，每個各等 60 秒的話光上傳就 720 秒，
# 遠超過前端給的 240 秒。改成整批共用一個截止時間。
# thread-local 而不是模組全域：這是多執行緒伺服器，兩個同時送單會互相
# 蓋掉，而且殘留的截止時間會害到後面單獨呼叫 comfy_upload 的地方。
COMFY_UP = threading.local()


def comfy_upload(name, blob, ctype="image/jpeg"):
    """multipart 上傳檔案到 ComfyUI input 資料夾。
    /upload/image 不檢查型別也不看副檔名，音訊走同一條路（欄位名仍須是 image）。"""
    import urllib.request as _u
    bnd = "----h3webui%d" % int(time.time() * 1000)
    body = io.BytesIO()
    def w(t): body.write(t if isinstance(t, bytes) else t.encode())
    w(f"--{bnd}\r\n")
    w(f'Content-Disposition: form-data; name="image"; filename="{name}"\r\n')
    w("Content-Type: %s\r\n\r\n" % ctype); w(blob); w("\r\n")
    w(f"--{bnd}\r\n")
    w('Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n')
    w(f"--{bnd}--\r\n")
    req = _u.Request(COMFY_URL + "/upload/image", data=body.getvalue(),
                     headers={"Content-Type": "multipart/form-data; boundary=" + bnd})
    left = 60.0
    dl = getattr(COMFY_UP, "deadline", 0.0)
    if dl:
        left = dl - time.time()
        if left <= 1:
            raise SubmitError("上傳素材超過時限（整批 120 秒）", 504)
    return json.loads(_u.urlopen(req, timeout=min(60.0, left)).read())


# ============================== 慣性偵測（第 1 層，純統計） ==============================
# 目標：找出「跨許多**不同**圖片」重複出現的動作片語 — 那些不是從圖片來的，是模型的慣性。
# 同一張圖的多個版本只算一次（最新版），硬切 / 一鏡到底分開統計。

_HB_SENT_DROP = re.compile(
    r"^\s*(?:the camera\b|no additional people|no people beyond|preserve\b|same (?:person|woman|man)\b"
    r"|her lips move|his lips move|their lips move|for the target video)", re.I)
_HB_STRIP = [
    (re.compile(r"^.*?\[Shot 1\]\s*", re.S), ""),            # alignment line / LoRA MAIN 前綴
    (re.compile(r"\[Shot \d+\]"), " "),
    (re.compile(r"At \d{2}:\d{2}\.\d{3},?"), " "),
    (re.compile(r"<d>.*?</d>", re.S), " "),                     # 台詞內容不算動作
    (re.compile(r"<[^>]{1,40}>"), " "),                         # <Picture 1> 等標籤
    (re.compile(r"\(S\d+(?:,S\d+)*\)"), " "),
    (re.compile(r"[Tt]he camera\b[^.;:]*"), " "),               # 句中運鏡子句（句首的整句另有 _HB_SENT_DROP）
    (re.compile(r"(?:her|his|their) lips move in (?:natural )?sync with the spoken words\.?", re.I), " "),
    (re.compile(r"while (?:her|his|their) lips remain (?:completely )?closed\.?", re.I), " "),
    # 身分錨定頭（風格宣告 + 構圖 + 識別句）與 preserving 子句是格式，不是劇情
    (re.compile(r"^[^.]*?(?:shown in|as established by)\s*(?:[Pp]icture\s*\d+)?,?\s*"), ""),
    (re.compile(r",?\s*preserving\b[^.]*"), ""),
]
# 這些詞屬於格式樣板（風格宣告/構圖/運鏡詞彙），不是「劇情動作」，含它們的片語不列入
_HB_BLOCK = {"camera", "amplitude", "speed", "shot", "picture", "animated", "anime", "cinematic",
             "live-action", "illustration", "style", "cg", "claymation", "watercolor", "framing",
             "close-up", "medium", "waist-up", "wide", "static", "preserve", "appearance",
             "clothing", "layout", "additional", "people", "lips", "sync", "spoken", "voiceover",
             "seconds", "timestamp", "integrated_multimodal_description", "overall_soundscape",
             "non_diegetic_music", "cut", "cuts", "hard", "established", "referenced", "aligns",
             "target", "video", "shown", "frame", "angle", "view", "profile",
             "voice", "says", "say", "saying", "speaks", "spoken", "sync"}
_HB_IDTAG = re.compile(r"\b(?:woman|women|man|men|girl|girls|boy|figure|lady|maid|character) in (?:the|a|an|her|his)\b")
_HB_FUNC = {"the", "a", "an", "her", "his", "their", "she", "he", "they", "it", "its", "and", "or",
            "with", "as", "to", "of", "in", "on", "at", "into", "from", "for", "then", "while",
            "is", "are", "was", "were", "one", "same", "that", "this", "up", "down", "out", "off",
            "by", "before", "after", "over", "under", "toward", "towards", "against", "across",
            "around", "through", "during", "onto", "beside", "behind", "still", "now", "just",
            "slightly", "moment", "later", "finally", "begins", "starts", "continues"}


_HB_CAM_CLAUSE = re.compile(r"[Tt]he camera\b[^.;:]*")


def _habit_cam_sigs(content):
    """運鏡簽名：動詞＋目標（去掉幅度/速度/尾句）。static shot 是規定的開場，不列。"""
    sigs = set()
    for cl in _HB_CAM_CLAUSE.findall(content or ""):
        t = cl.lower()
        t = re.sub(r"\s*with (?:small|large|medium)\s+amplitude", "", t)
        t = re.sub(r"\s*at (?:slow|fast|normal|moderate)\s+speed", "", t)
        t = re.split(r"\b(?:as|while|until|revealing|so)\b", t)[0]
        t = re.sub(r"^the camera\s+", "", t)
        t = re.sub(r"\s+", " ", t).strip(" .,-")
        if t and "static shot" not in t and len(t.split()) >= 2:
            sigs.add(t)
    return sigs


def _habit_prose(content):
    """imd 欄位 -> 只留劇情動作的散文（去掉運鏡句、鎖定句、台詞、標記）。"""
    imd = content or ""
    i = imd.find("integrated_multimodal_description:")
    j = imd.find("overall_soundscape:")
    if i >= 0:
        imd = imd[i + len("integrated_multimodal_description:"): j if j > i else None]
    for rx, sub in _HB_STRIP:
        imd = rx.sub(sub, imd)
    sents = [x.strip() for x in re.split(r"(?<=[.!?])\s+", imd) if x.strip()]
    return " ".join(x for x in sents if not _HB_SENT_DROP.match(x))


def _habit_grams(prose):
    """一份文件的 3–5 字 n-gram 集合（正規化小寫；擋樣板詞與純虛詞片語）。"""
    words = re.findall(r"[a-z][a-z'-]*", prose.lower())
    out = set()
    for n in (3, 4, 5):
        for k in range(len(words) - n + 1):
            g = words[k:k + n]
            if any(w in _HB_BLOCK for w in g):
                continue
            if sum(1 for w in g if w not in _HB_FUNC) < 2:
                continue
            gs = " ".join(g)
            if _HB_IDTAG.search(gs):                   # 識別句（the woman in the …）是規定格式，不是慣性
                continue
            out.add(gs)
    return out


def habit_stats(mode, max_images=30, min_images=5):
    """回傳 {images, phrases:[{p, n, pct}]} — 只看每張圖的最新版本，最多 max_images 張。"""
    rows = load_index()
    docs = {}
    for row in rows:                                   # index 新的在前
        if (row.get("mode") or "cuts") != mode:
            continue
        key = row.get("upload_id") or row.get("image") or row.get("id")
        if key in docs:
            continue
        rp = os.path.join(HIST, str(row.get("id")) + ".json")
        try:
            with open(rp, encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:
            continue
        prose = _habit_prose(rec.get("content", ""))
        docs[key] = (_habit_grams(prose), " ".join(re.findall(r"[a-z][a-z'-]*", prose.lower())),
                     _habit_cam_sigs(rec.get("content", "")))
        if len(docs) >= max_images:
            break
    n = len(docs)
    if n < min_images:
        return {"images": n, "phrases": [], "camera": [], "note": "紀錄不足 %d 張不同圖片，暫不啟用" % min_images}
    df = {}
    for grams, _, _ in docs.values():
        for g in grams:
            df[g] = df.get(g, 0) + 1
    texts = [t for _, t, _ in docs.values()]
    camdf = {}
    for _, _, sigs in docs.values():
        for g in sigs:
            camdf[g] = camdf.get(g, 0) + 1

    def dfreq(g):
        pat = " " + g + " "
        return sum(1 for t in texts if pat in (" " + t + " "))

    def expand(g):
        """沿實際文本左右擴張，只要擴張後的片語出現次數仍過線就繼續 — 還原完整的慣性句。"""
        for _ in range(12):
            grew = False
            for side in ("right", "left"):
                votes = {}
                for t in texts:
                    tt = " " + t + " "
                    start = 0
                    while True:
                        i = tt.find(" " + g + " ", start)
                        if i < 0:
                            break
                        if side == "right":
                            rest = tt[i + len(g) + 2:].split(" ", 1)[0]
                        else:
                            rest = tt[:i].rstrip().rsplit(" ", 1)[-1]
                        if rest:
                            votes[rest] = votes.get(rest, 0) + 1
                        start = i + 1
                if not votes:
                    continue
                w = max(votes, key=votes.get)
                if w in _HB_BLOCK:                     # 擴張不得長進樣板詞（運鏡/對白/鎖定句詞彙）
                    continue
                g2 = (g + " " + w) if side == "right" else (w + " " + g)
                if dfreq(g2) >= thresh:
                    g = g2
                    grew = True
            if not grew:
                break
        return g
    thresh = max(3, -(-n * 30 // 100))                 # ceil(30%)
    cands = sorted(((g, c) for g, c in df.items() if c >= thresh),
                   key=lambda x: (-x[1], -len(x[0].split())))
    # 同一個慣性會產生一整串滑動重疊的 n-gram（turns to face the / to face the lens ...）
    # 用「共享任何連續 2 字窗」判定重疊，每個慣性家族只留出現次數最高的一條代表
    def shingles(g):
        w = g.split()
        return {" ".join(w[i:i + 2]) for i in range(len(w) - 1)}
    kept = []
    for g, c in cands:
        if any(g in kg for kg, _, _ in kept):          # 已被更長的擴張句涵蓋
            continue
        sh = shingles(g)
        if any(sh & ksh for _, _, ksh in kept):
            continue
        eg = expand(g)
        kept.append((eg, dfreq(eg), shingles(eg)))
    kept = [(g, c) for g, c, _ in kept]
    kept.sort(key=lambda x: -x[1])
    cam = sorted(((g, c) for g, c in camdf.items() if c >= thresh), key=lambda x: -x[1])
    camkept = []
    for g, c in cam:                                       # 子字串去重（pushes in on her face / pushes in on her）
        if any(g in kg or kg in g for kg, _ in camkept):
            continue
        camkept.append((g, c))
    return {"images": n,
            "phrases": [{"p": g, "n": c, "pct": round(c * 100 / n)} for g, c in kept[:8]],
            "camera": [{"p": g, "n": c, "pct": round(c * 100 / n)} for g, c in camkept[:5]]}


def _les_norm(t):
    return re.sub(r"[\W_]+", "", (t or "").lower())


def load_lessons():
    try:
        with open(LESSONS_FILE, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def save_lessons(rows):
    with open(LESSONS_FILE, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)


REQUIRED_DEFAULT = {"min_words": 0, "lang_lock": "any", "line_min": 0, "line_max": 0}


def load_required():
    try:
        with open(REQUIRED_FILE, encoding="utf-8") as f:
            v = json.load(f)
        return dict(REQUIRED_DEFAULT, **(v if isinstance(v, dict) else {}))
    except Exception:
        return dict(REQUIRED_DEFAULT)


def save_required(v):
    with open(REQUIRED_FILE, "w", encoding="utf-8") as f:
        json.dump(v, f, ensure_ascii=False, indent=1)


def comfy_template_from_json(body):
    """把使用者上傳的 workflow JSON 正規化成模板並驗證。
    接受：本工具的抓取格式 {"graph", "extra_data"}、或 ComfyUI「Export (API)」的節點圖。
    UI 格式（Save 存的 nodes/links 檔）無法直接送生成，回明確指引。"""
    if not isinstance(body, dict):
        raise ValueError("不是 JSON 物件")
    if isinstance(body.get("graph"), dict):
        g, extra = body["graph"], (body.get("extra_data") if isinstance(body.get("extra_data"), dict) else {})
    elif isinstance(body.get("nodes"), list):
        # UI 格式（Save 存的 nodes/links）：伺服器端直接轉成可執行的 API 圖 ——
        # 不需要先在 ComfyUI 跑過一次。原 UI 檔同時掛為影片內嵌 metadata。
        try:
            oi = comfy_api("/object_info", timeout=30)
        except Exception as e:
            raise ValueError("轉換 UI 工作流需要連上 ComfyUI 讀節點定義（/object_info）：%s" % e)
        import ui2api
        g, warns = ui2api.ui_to_api(body, oi)
        for w in warns:
            sys.stderr.write("  ui2api: %s\n" % w)
        extra = {"extra_pnginfo": {"workflow": body}}
    elif body and all(isinstance(v, dict) and "class_type" in v for v in body.values()):
        g, extra = body, {}
    else:
        raise ValueError("看不懂的格式：既不是 Export (API) 的節點圖，也不是本工具的模板檔")
    director = None
    for node in g.values():
        if node.get("class_type") == "MiniMaxH3Director":
            director = node
    if director is None:
        raise ValueError("工作流裡沒有 MiniMaxH3Director 節點")
    ins = director.get("inputs", {})
    for k in ("builder_state", "timeline_data", "mode"):
        if k not in ins:
            raise ValueError("MiniMaxH3Director 缺少輸入 %s（請用 Export (API) 匯出，不要手改）" % k)
    try:
        tl = json.loads(ins.get("timeline_data") or "{}")
    except Exception:
        raise ValueError("timeline_data 不是合法 JSON")
    # timeline 允許沒有圖片：送出時會依模式（I2VA/FL2VA/L2VA/REF2VA）重建圖片清單，
    # 所以存檔當下是 T2VA、items 為空的工作流也是合法模板。
    carried = False
    if not ((extra.get("extra_pnginfo") or {}).get("workflow") or {}).get("nodes"):
        # Export (API) 檔沒有 UI metadata（影片內嵌 workflow 靠它）。如果現有模板有、
        # 而且節點組成一致（= 同一個工作流），就沿用舊的 UI metadata，內嵌不中斷。
        try:
            with open(COMFY_TEMPLATE, encoding="utf-8") as f:
                old_tpl = json.load(f)
            old_extra = old_tpl.get("extra_data") or {}
            old_g = old_tpl.get("graph") if isinstance(old_tpl.get("graph"), dict) else {}
            if ((old_extra.get("extra_pnginfo") or {}).get("workflow") or {}).get("nodes"):
                if sorted(n.get("class_type", "") for n in old_g.values()) == \
                   sorted(n.get("class_type", "") for n in g.values()):
                    extra = old_extra
                    carried = True
        except Exception:
            pass
    if os.path.exists(COMFY_TEMPLATE):
        try:
            os.replace(COMFY_TEMPLATE, COMFY_TEMPLATE + ".bak")
        except OSError:
            pass
    with open(COMFY_TEMPLATE, "w", encoding="utf-8") as f:
        json.dump({"graph": g, "extra_data": extra}, f, ensure_ascii=False, indent=1)
    return {"nodes": len(g), "mode": ins.get("mode", "?"),
            "has_ui_meta": bool(((extra.get("extra_pnginfo") or {}).get("workflow") or {}).get("nodes")),
            "ui_meta_carried": carried,
            "backup": os.path.exists(COMFY_TEMPLATE + ".bak")}


def comfy_capture_template():
    """抓 ComfyUI 最近一次成功生成的圖存成模板"""
    hist = comfy_api("/history?max_items=40")
    best = best_wf = None
    for pid, rec in hist.items():          # 插入順序 = 送出順序，越後越新
        st = rec.get("status", {})
        if st.get("status_str") == "success" and st.get("completed"):
            pr = rec.get("prompt") or []
            g = pr[2] if len(pr) > 2 else None
            if g and any(n.get("class_type") == "MiniMaxH3Director" for n in g.values()):
                best = (pid, g, rec)
                ed = pr[3] if len(pr) > 3 and isinstance(pr[3], dict) else {}
                if ((ed.get("extra_pnginfo") or {}).get("workflow") or {}).get("nodes"):
                    best_wf = (pid, g, rec)   # 帶 UI 工作流的才有完整 metadata 可嵌
    best = best_wf or best
    if not best:
        return None
    pid, g, rec = best
    pr = rec.get("prompt") or []
    extra = pr[3] if len(pr) > 3 and isinstance(pr[3], dict) else {}
    with open(COMFY_TEMPLATE, "w", encoding="utf-8") as f:
        json.dump({"graph": g, "extra_data": extra}, f, ensure_ascii=False, indent=1)
    out = ""
    for nid, o in (rec.get("outputs") or {}).items():
        for k, v in o.items():
            if isinstance(v, list):
                for it in v:
                    if isinstance(it, dict) and str(it.get("filename", "")).lower().endswith(VIDEO_EXT):
                        out = it["filename"]
    return {"prompt_id": pid, "nodes": len(g), "output": out}


# 畫質預設 -> 百萬像素。數值抄自 ComfyUI Director 前端的 RESOLUTION_PRESETS
# （custom_nodes/ComfyUI-DaSiWa-Nodes/js/minimax_h3_director.js），與它算出的畫布一致：
#   pixels = MP * 1024 * 1024 ; h = sqrt(pixels/aspect) ; w = h*aspect ; 兩邊對齊 32
RESOLUTION_MP = {
    "144p": 0.0352, "240p": 0.0977, "360p": 0.22, "480p": 0.391, "540p": 0.494, "576p": 0.396,
    "720p": 0.879, "900p": 1.373, "1024p": 1.00, "1080p": 1.978, "1152p": 2.25, "1440p": 3.516,
    "2160p": 7.91, "2K": 3.906, "4K": 7.91,
    "0.26 MP - Preview": 0.26, "0.36 MP - Small": 0.36, "0.52 MP - SD": 0.52,
    "0.65 MP - Balanced": 0.65, "0.83 MP - HD": 0.83, "1.00 MP - 1024p": 1.00,
    "1.05 MP - HD+": 1.05, "1.20 MP - HD++": 1.20, "1.35 MP - 2K lite": 1.35,
    "1.55 MP - 2K": 1.55, "1.65 MP - 2K+": 1.65, "1.75 MP - QHD": 1.75,
    "2.10 MP - FHD": 2.10, "3.30 MP - QHD+": 3.30, "4.75 MP - 2K Pro": 4.75,
    "6.50 MP - Production": 6.50, "8.30 MP - UHD": 8.30,
}
NATIVE_SHORT_EDGE = "Native (ShortEdge 768px)"     # Director 的 "auto"：短邊固定 768
RES_PRESETS = [NATIVE_SHORT_EDGE] + list(RESOLUTION_MP)


ASPECT_RE = re.compile(r"^\s*(\d{1,4})\s*[:/x×]\s*(\d{1,4})\s*$")


def aspect_ratio(aspect):
    """"2:3" -> 0.667。認不得就回 None（代表改用輸入圖的比例）。"""
    if isinstance(aspect, (int, float)) and aspect > 0:
        return float(aspect)
    m = ASPECT_RE.match(str(aspect or ""))
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    return (w / h) if (w > 0 and h > 0) else None


def fit_canvas(img_w, img_h, base_w, base_h, preset=None, mult=32, aspect=None):
    """依輸入圖的長寬比算出畫布尺寸，像素預算沿用模板的 width*height。
    v18 的 timeline_data.resolution.aspect="auto" 只有 ComfyUI 的瀏覽器 JS 會執行；
    我們是直接把 API 圖 POST 給 /prompt，沒有瀏覽器，Director 只會用 width/height
    這兩個 widget 的字面值。所以「依圖決定比例」必須由這裡算好再寫進去。

    對齊必須是 32：VAE 先把邊長 /16 成 latent，模型的 patchify_video 再以 patch_size=2
    切塊，所以 latent 的長寬都得是偶數。用 16 對齊會產生奇數 latent（例如 656x848 ->
    41x53），SamplerCustomAdvanced 會丟 "shape ... is invalid for input of size ..."。"""
    try:
        img_w, img_h = int(img_w or 0), int(img_h or 0)
        base_w, base_h = int(base_w), int(base_h)
    except (TypeError, ValueError):
        return None
    if min(base_w, base_h) <= 0:
        return None
    # 指定了比例就用它；沒有才回頭看輸入圖（T2VA 沒有圖，只有指定比例時才算得出來）
    ratio = aspect_ratio(aspect)
    if ratio is None:
        if min(img_w, img_h) <= 0:
            return None
        ratio = img_w / img_h
    if preset == NATIVE_SHORT_EDGE:               # 短邊固定 768，長邊依比例
        short = 768
        w, h = (short * ratio, short) if ratio >= 1 else (short, short / ratio)
    else:
        mp = RESOLUTION_MP.get(preset)
        # 有選畫質就用它的像素預算，沒選就沿用模板現有的畫布大小
        budget = (mp * 1024 * 1024) if mp else (base_w * base_h)
        w = (budget * ratio) ** 0.5
        h = w / ratio if ratio else 0
    # 長寬各自取整會把比例帶歪（16:9 的 979x551 -> 992x544 = 1.824，差 2.6%）。
    # 從鄰近的對齊候選裡挑比例最準的，同分再看誰的像素數貼近預算。
    # 使用者明確指定比例時放寬搜尋範圍並容忍 ±12% 的像素預算落差——比例是他挑的，
    # 要優先保住；auto 的「目標比例」本來就是圖片自己，差個 1~2% 看不出來，
    # 那就守住預算不亂縮。
    budget = w * h
    span = (-2, -1, 0, 1, 2) if aspect else (0, 1)
    tol = 0.12 if aspect else 1.0
    bw, bh = int(w // mult), int(h // mult)
    best = None
    for dw in span:
        for dh in span:
            cw = max(mult, (bw + dw) * mult)
            chh = max(mult, (bh + dh) * mult)
            area = cw * chh
            if abs(area / budget - 1.0) > tol:
                continue
            err = abs((cw / chh) / ratio - 1.0)
            key = (round(err, 6), abs(area - budget))
            if best is None or key < best[0]:
                best = (key, cw, chh)
    if best is None:                      # 容忍度把候選全濾掉了：退回單純取整
        return (max(mult, int(round(w / mult)) * mult),
                max(mult, int(round(h / mult)) * mult))
    return best[1], best[2]


def apply_wf_params(g, wf):
    """把工作流預設參數寫進節點圖（v18）。缺項就不動模板值。
    比例／畫質／放大解析度一律不碰——它們在 Director 的 timeline_data.resolution，
    模板預設 aspect="auto"（依輸入圖自動決定），這正是我們要的行為。"""
    wf = wf if isinstance(wf, dict) else {}
    for nid, node in g.items():
        ct = node.get("class_type")
        ins = node.get("inputs", {})
        if ct == "BasicScheduler" and wf.get("steps"):
            try:
                v = int(wf["steps"])
                if 1 <= v <= 100:
                    ins["steps"] = v
            except (TypeError, ValueError):
                pass
        if ct == "MiniMaxH3SigmaShift":
            for k in ("shift_video", "shift_audio"):
                if wf.get(k) is not None and str(wf.get(k)) != "":
                    try:
                        v = float(wf[k])
                        if 0.01 <= v <= 100:
                            ins[k] = v
                    except (TypeError, ValueError):
                        pass
        # v18：frame_rate 已移到 Director 上（模板裡預設是空字串，送單前必須給實數）
        if ct == "MiniMaxH3Director":
            fr = ins.get("frame_rate")
            v = None
            if wf.get("fps"):
                try:
                    fv = float(wf["fps"])
                    if 1 <= fv <= 120:
                        v = fv
                except (TypeError, ValueError):
                    v = None
            if v is None and not isinstance(fr, (int, float)):
                v = 24.0                      # 模板留空 -> 用節點預設值，否則 ComfyUI 會擋下
            if v is not None:
                ins["frame_rate"] = v



# ---------------------------------------------------------------- shared settings
# 共用設定：以前這些散在各瀏覽器的 localStorage，換一台電腦或換瀏覽器就要重設一遍。
# 只有純視覺、真的因裝置而異的（介面語言、側欄收合）才留在本機。
SHARED_DEFAULTS = {
    "h3.settings.v1": None,     # 生成參數（重試次數、翻譯開關…）
    "h3.review.v1": None,       # 審查設定（及格分數、重試上限、逐模式開關）
    "h3.reviewsys.v1": None,    # 審查 system prompt
    "h3.sp.v1": None,           # 生成 system prompt 相關
    "h3.t2vasp.v1": None,       # T2VA 潤飾 system prompt
    "h3.skills.v1": None,       # 啟用的 skill 組合
    "h3.styleskill.v1": None,   # 舊版單選風格（相容）
    "h3.prompt.all.v1": None,   # 選用的 prompt 組
    "h3.refrule.v1": None,      # REF2VA：參考圖只當外觀依據的疊加規則
    # 前端從 v2.1 起就把這兩個當共用設定送上來，但這裡沒列，伺服器一直默默丟掉——
    # 換台電腦側欄與停留的頁面還是回到預設，等於那次「改成共用」從來沒生效。
    "h3.side.collapsed.v1": None,
    "h3.view.v1": None,
    "h3.lora.req.v1": None,     # LoRA（必須）：每次送 ComfyUI 都會套用，最多 5 個
    "h3.lora.recipes.v1": None, # LoRA（配方）：最多 20 組，每組最多 10 個，各帶自己的觸發詞
    "h3.lora.reqprompt.v1": None,  # LoRA（必須）的觸發詞
}


def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {"rev": 0, "values": {}}
    except Exception:
        return {"rev": 0, "values": {}}


def save_settings(values, rev):
    doc = {"rev": rev, "values": values, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    with LOCK:
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        os.replace(tmp, SETTINGS_PATH)
    return doc



def load_drafts():
    try:
        with open(DRAFTS_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_drafts(d):
    with LOCK:
        tmp = DRAFTS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, DRAFTS_PATH)
    return d


# ---------------------------------------------------------------- queue store
# 送 ComfyUI 的唯一真相。不能倚賴 ComfyUI 的 /history：那是記憶體字典，一重啟就清空
# （實測輸出資料夾 2700+ 個檔案，/history 只剩 5 筆）。每筆任務一個 queue/<jid>.json。
def qcompact(row):
    return {k: row.get(k) for k in
            ("id", "ts", "state", "mode", "title", "rec_id", "prompt_id",
             "video", "first_frame", "error", "t_submit", "t_end")}


def qsave(row):
    """單筆落地 + 重寫索引。呼叫端須持有 QLOCK。"""
    jid = row["id"]
    QROWS[jid] = row
    tmp = os.path.join(QUEUE, jid + ".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(row, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(QUEUE, jid + ".json"))
    with LOCK:
        tmp = QINDEX + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted([qcompact(r) for r in QROWS.values()],
                             key=lambda r: r.get("ts") or 0, reverse=True)[:500],
                      f, ensure_ascii=False)
        os.replace(tmp, QINDEX)


def qload_all():
    """啟動時把磁碟任務讀回記憶體；中斷的任務標回可續狀態。"""
    try:
        names = os.listdir(QUEUE)
    except OSError:
        return
    with QLOCK:
        for fn in names:
            if not fn.endswith(".json") or fn == "index.json":
                continue
            try:
                with open(os.path.join(QUEUE, fn), encoding="utf-8") as f:
                    r = json.load(f)
                if isinstance(r, dict) and r.get("id"):
                    if r.get("state") in ("submitting", "running"):
                        # 伺服器重啟時仍在跑：有 prompt_id 就交給 reconcile 確認，否則重排
                        r["state"] = "running" if r.get("prompt_id") else "queued"
                    QROWS[r["id"]] = r
            except Exception:
                pass


def qrow(jid):
    with QLOCK:
        r = QROWS.get(jid)
        return dict(r) if r else None


def qset(jid, **fields):
    with QLOCK:
        r = QROWS.get(jid)
        if not r:
            return None
        r.update(fields)
        qsave(r)
        return dict(r)


class SubmitError(Exception):
    def __init__(self, msg, code=502):
        super().__init__(msg); self.msg = msg; self.code = code


def _node_errors_text(ne, limit=6):
    """把 ComfyUI 的 node_errors 攤成幾行看得懂的字：哪個節點、哪個欄位、錯在哪。"""
    lines = []
    for nid, info in list(ne.items())[:limit]:
        info = info or {}
        lines.append("節點 %s %s" % (nid, info.get("class_type") or ""))
        for e in (info.get("errors") or [])[:4]:
            det = e.get("details") or ""
            lines.append("  - %s%s" % (e.get("message") or "", (": " + det) if det else ""))
    if len(ne) > limit:
        lines.append("  …另有 %d 個節點" % (len(ne) - limit))
    return chr(10).join(lines)[:900]


# ------------------------------------------------------------------ 去背服務
# 獨立環境（E:/h3-matte）跑一支小服務，按需啟動、閒置自動卸模型與結束。
# 裡面同時有 BiRefNet（自動去背，前端在用）與 SAM2（點選分割，目前前端沒用）。
# 分開的理由：它跟 ComfyUI 搶同一張卡，不用時要能把 VRAM 整個放掉；
# 而且不該為了省磁碟去動 ComfyUI 那個環境的 numpy/opencv。
CUT_DIR = os.environ.get("CUT_DIR", r"E:/h3-matte")
CUT_PORT = int(os.environ.get("CUT_PORT", "9996"))
# 去背服務由別人管的時候（docker compose 裡的另一個容器）給完整網址。
# 這時不會 subprocess 拉它——容器裡根本沒有那個 venv——只等它回應。
# 沒給就照舊：本機按需啟動、閒置自己結束。
CUT_EXTERNAL = os.environ.get("CUT_URL", "").strip().rstrip("/")
CUT_URL = CUT_EXTERNAL or "http://127.0.0.1:%d" % CUT_PORT
# 去背權重：ComfyUI 在同一台時直接用它那份，沒必要再複製。
# ComfyUI 搬到別台之後就只能自己放一份——放在 repo 的 models/ 底下（不進 git）。
_MATTE_LOCAL = os.path.join(ROOT, "models", "background_removal")
MATTE_DIR = os.environ.get("BREF_MODELS") or (
    _MATTE_LOCAL if os.path.isdir(_MATTE_LOCAL)
    else "E:/ComfyUI-MiniMaxH3/ComfyUI/models/background_removal")
MATTE_DEFAULT = "birefnet.safetensors"


def matte_models():
    """列可用的去背模型。只讀目錄，不碰 GPU 服務——
    設定頁要用這份清單，不能一開設定就把模型載進 VRAM。"""
    out = []
    try:
        for fn in sorted(os.listdir(MATTE_DIR)):
            if not fn.endswith(".safetensors"):
                continue
            low = fn.lower()
            out.append({"name": fn,
                        "mb": round(os.path.getsize(os.path.join(MATTE_DIR, fn)) / 1048576.0),
                        "image_size": 2048 if ("hr" in low or "2048" in low) else 1024})
    except OSError:
        pass
    return out
CUT_PY = os.path.join(CUT_DIR, ".venv", "Scripts", "python.exe")
CUT_APP = os.path.join(CUT_DIR, "matte_service.py")
CUT_LOG = os.path.join(ROOT, "h3-matte.log")
CUT_LOCK = threading.RLock()
# CUT_LOCK 管的是「把服務拉起來」；CUT_BUSY 管的是「一次只送一張進去」。
# 兩件事不能共用一把鎖：拉服務要阻塞等，送圖要立刻拒絕。
# 去背服務端本來就是全程序一把鎖，這裡不擋的話第二張只是排在
# 對方的鎖後面，瀏覽器看不出差別，等滿逾時換下一張。
CUT_BUSY = threading.Semaphore(1)


def cut_installed():
    if CUT_EXTERNAL:
        return True
    return os.path.exists(CUT_PY) and os.path.exists(CUT_APP)


def cut_ping(timeout=2):
    try:
        with urllib.request.urlopen(CUT_URL + "/health", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def cut_ensure(wait=40):
    """服務沒在跑就拉起來。它自己閒置 15 分鐘會結束，所以這裡要能重複拉。"""
    h = cut_ping()
    if h:
        return h
    if CUT_EXTERNAL:
        # 容器重啟要幾秒，等一下就好；但不能照本機啟動的 40 秒等，
        # 容器真的倒了的話那只是讓人多盯著轉圈。
        t0 = time.time()
        while time.time() - t0 < min(wait, 15):
            time.sleep(0.6)
            h = cut_ping()
            if h:
                return h
        raise SubmitError("去背服務（%s）沒有回應，請確認容器有在跑" % CUT_EXTERNAL, 503)
    if not cut_installed():
        raise SubmitError("去背服務環境還沒安裝（預期在 %s）" % CUT_DIR, 501)
    # 不帶上限的 with CUT_LOCK 會讓 wait 形同虛設：排在別人後面的人可能先
    # 花掉對方的 40 秒，自己的 8 秒才開始算。拿不到鎖就直接講，別默默等。
    if not CUT_LOCK.acquire(timeout=max(1.0, wait)):
        raise SubmitError("去背服務正在啟動中，稍後再試", 503)
    try:
        h = cut_ping()
        if h:
            return h
        try:
            # 以前它的輸出直接丟進 DEVNULL，去背出事時什麼都查不到。
            # 改成寫檔，並把路徑印在主控台上。
            lf = open(CUT_LOG, "ab", buffering=0)
            slog("  [cut] 啟動去背服務，log：%s" % CUT_LOG)
            # 權重位置由這邊決定再交給它，兩邊才不會各自猜出不同的資料夾
            # （這邊的下拉選單列的是 MATTE_DIR，它載的卻是自己預設的那份）。
            subprocess.Popen([CUT_PY, CUT_APP], cwd=CUT_DIR,
                             env=dict(os.environ, BREF_MODELS=MATTE_DIR),
                             stdout=lf, stderr=lf,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            raise SubmitError("去背服務啟動失敗: %s" % e, 500)
        t0 = time.time()
        while time.time() - t0 < wait:
            time.sleep(0.6)
            h = cut_ping()
            if h:
                return h
    finally:
        CUT_LOCK.release()
    raise SubmitError("去背服務啟動逾時", 504)


def cut_unload(timeout=20):
    """叫去背服務把模型從 VRAM 放掉。

    它閒置三分鐘會自己卸，但交接的時候等不了那麼久——去完背接著
    按生成，使用者看到的就是三分鐘的不明等待。服務沒在跑就不用拉它起來，
    沒在跑就沒占 VRAM。"""
    if not cut_ping(timeout=2):
        return None
    try:
        with urllib.request.urlopen(CUT_URL + "/unload", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


# host.docker.internal：h3-server 在容器裡、ComfyUI／LLM 在同一台主機上時用的名字。
# 那是同一張卡，不能因為名字不像本機就當成別台而放行。
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "",
               "host.docker.internal", "gateway.docker.internal"}


def is_local_url(u):
    """網址指向本機嗎？去背服務是 h3-server 自己 subprocess 起的，
    所以只有「對方也在本機」時才真的會搶同一張卡。"""
    try:
        h = (urlparse(u or "").hostname or "").lower()
    except Exception:
        return True                        # 解不開就當成本機，寧可保守
    return h in LOCAL_HOSTS


def cut_rivals_local():
    """回傳 (comfy 在本機, llama 在本機)。"""
    return (is_local_url(CONFIG.get("comfy_url")), is_local_url(CONFIG.get("llama_url")))


def cut_gpu_block():
    """GPU 忙就擋下來。不排隊——編輯圖片時沒人想等算圖跑完。
    回傳擋下的理由，空字串＝放行。

    設定有三種：auto（預設）、"0" 一律擋、"1" 一律放行。
    auto 看對方在不在本機：ComfyUI / LLM 指向別台機器時根本不搶 GPU，
    擋下來只是無端不能用。"""
    comfy_here, llama_here = cut_rivals_local()
    try:
        vals = (load_settings() or {}).get("values") or {}
        g = vals.get("h3.settings.v1")
        if isinstance(g, str):
            g = json.loads(g)
        # cutBusy 是現在的名字，samBusy 是改名前的——舊設定要繼續認，
        # 不然使用者不會知道自己開過的選項默默失效了。
        mode = str((g or {}).get("cutBusy") or (g or {}).get("samBusy") or "auto")
    except Exception:
        mode = "auto"
    if mode == "1":
        return ""                          # 使用者強制允許同時使用
    if mode == "auto":
        # 兩個都在別台機器：沒有任何矛盾，直接放行
        if not comfy_here and not llama_here:
            return ""
        if LLAMA_INFLIGHT[0] > 0 and llama_here:
            return "LLM 正在生成劇情，GPU 忙碌中"
        if comfy_here:
            b = comfy_busy()
            if b:
                return "ComfyUI 正在算圖，GPU 忙碌中"
            # 不知道就當成忙。這條守門的全部意義就是「別在對方算圖時去碰同一張卡」，
            # 猜錯的代價是整台機器一起慢。
            if b is None:
                return "ComfyUI 沒在期限內回應佇列查詢（多半正忙），GPU 狀態不明"
        return ""
    # mode == "0"：不管在哪一台，對方忙就擋
    if LLAMA_INFLIGHT[0] > 0:
        return "LLM 正在生成劇情，GPU 忙碌中"
    b = comfy_busy()
    if b:
        return "ComfyUI 正在算圖，GPU 忙碌中"
    if b is None:
        return "ComfyUI 沒在期限內回應佇列查詢（多半正忙），GPU 狀態不明"
    return ""




# ---------- SAM3 person 閘 ----------
# BiRefNet 是「顯著物件」分割，人坐在床上床就算主體——這是語意判斷，
# 拉高解析度與換權重都量過，救不了。text prompt 的分割器根本沒有
# 「顯著性」這個概念：說 person 就只有 person。
SAM3_WF = os.path.join(ROOT, "workflows", "SAM3_人物分離去背.json")
# 實測：一張畫面兩個人時，光寫 person 只抓到其中一個（覆蓋 27.6%），
# person:2 兩個都抓到（65.2%）。:N 是注意力權重不是人數，而且會飽和——
# :99 跟 :2 的結果一模一樣；畫面只有一個人時 person / :2 / :99 也完全相同。
SAM3_PROMPT_DEF = "person:2"


def sam3_ready():
    """能不能用。任何一環不在就回 False，前端連選項都不會顯示。"""
    if not os.path.exists(SAM3_WF):
        return False
    try:
        return bool(comfy_api("/object_info/SAM3_Detect", timeout=6))
    except Exception:
        return False


def sam3_mask(durl, prompt="", thr=0.3):
    """跑使用者自己建的那個工作流，回一張帶 alpha 的 PNG dataURL。

    individual_masks 強制轉 False：它預設會每個人各一張遮罩，
    只拿到其中一張的話，畫面上另一個人會被當成背景刪掉——
    實測時就是這樣把紫髮那個角色整個弄不見的。"""
    from ui2api import ui_to_api
    import urllib.parse as _p
    with open(SAM3_WF, encoding="utf-8") as f:
        ui = json.load(f)
    api, _warn = ui_to_api(ui, comfy_api("/object_info", timeout=30))
    head, b64 = (durl.split(",", 1) + [""])[:2] if "," in durl else ("", durl)
    ext = "png" if "png" in head.lower() else "jpg"
    name = "h3_sam3_%d.%s" % (int(time.time() * 1000), ext)
    comfy_upload(name, base64.b64decode(b64), "image/" + ("png" if ext == "png" else "jpeg"))
    for node in api.values():
        ct = node.get("class_type")
        if ct == "LoadImage":
            node["inputs"]["image"] = name
        elif ct == "CLIPTextEncode":
            node["inputs"]["text"] = (prompt or SAM3_PROMPT_DEF).strip()
        elif ct == "SAM3_Detect":
            node["inputs"]["threshold"] = float(thr)
            node["inputs"]["individual_masks"] = False
    pid = comfy_api("/prompt", {"prompt": api}, timeout=60).get("prompt_id")
    if not pid:
        raise SubmitError("ComfyUI 沒收下這個工作流", 502)
    t0 = time.time()
    while time.time() - t0 < 300:
        time.sleep(0.5)
        h = comfy_api("/history/" + pid, timeout=30)
        if h.get(pid):
            break
    else:
        raise SubmitError("SAM3 跑太久（超過 300 秒）", 504)
    # 這個工作流有兩個輸出節點：PreviewImage（遮罩轉 RGB，沒有 alpha）
    # 與 SaveImage（RGBA）。拿到 RGB 那張的話 alpha 全是 255，前端會把它
    # 當成「全部都是主體」而翻成全空。按 PNG 的 IHDR 色彩類型挑：
    # 6=RGBA、4=灰階+alpha。不需要影像函式庫。
    best = None
    for _nid, d in (h[pid].get("outputs") or {}).items():
        for im in (d.get("images") or []):
            q = "?filename=%s&subfolder=%s&type=%s" % (
                _p.quote(im["filename"]), _p.quote(im.get("subfolder", "")),
                im.get("type", "output"))
            raw = urllib.request.urlopen(COMFY_URL + "/view" + q, timeout=120).read()
            ct = raw[25] if len(raw) > 25 and raw[1:4] == b"PNG" else -1
            if ct in (4, 6):
                return "data:image/png;base64," + base64.b64encode(raw).decode()
            if best is None:
                best = raw
    if best is not None:
        slog("  [sam3] 警告：所有輸出都沒有 alpha 通道，退而用第一張")
        return "data:image/png;base64," + base64.b64encode(best).decode()
    raise SubmitError("SAM3 沒有輸出圖片", 502)

def comfy_submit(body):
    """一筆送單 -> ComfyUI 的 prompt_id。/api/comfy/run 與佇列派工共用，
    確保兩條路徑的圖片解析、上傳、建圖與參數覆寫完全一致。"""
    if not isinstance(body, dict):
        raise SubmitError("body must be an object", 400)
    if not os.path.exists(COMFY_TEMPLATE):
        try:
            if not comfy_capture_template():
                raise SubmitError("沒有模板：先在 ComfyUI 成功跑一次工作流", 400)
        except SubmitError:
            raise
        except Exception as e:
            raise SubmitError("ComfyUI 連不上: %s" % e, 502)
    blob, ext = None, "jpg"
    rec_audio_blobs = []
    run_mode = str(body.get("mode") or "").lower()
    full_prompt = str(body.get("content") or "")
    extra_blobs = []
    rid = str(body.get("rec_id") or "")
    if rid and ID_RE.match(rid):
        op, oext = orig_path(HIST, rid)
        if op:
            blob, ext = open(op, "rb").read(), oext
        else:
            fp = os.path.join(HIST, rid + ".full.jpg")
            if os.path.exists(fp):
                blob = open(fp, "rb").read()
        try:
            with open(os.path.join(HIST, rid + ".json"), encoding="utf-8") as f:
                rec0 = json.load(f)
            if not run_mode:
                run_mode = str(rec0.get("mode") or "").lower()
            if not full_prompt:
                full_prompt = str(rec0.get("content") or "")
            for n in range(1, int(rec0.get("nmore") or 0) + 1):
                fp2 = os.path.join(HIST, "%s.x%d.jpg" % (rid, n))
                if os.path.exists(fp2):
                    extra_blobs.append(open(fp2, "rb").read())
            # 音訊也要一起回來，否則 prompt 裡的 <Audio N> 會指向不存在的東西
            for a in (rec0.get("audio") or [])[:MAX_REF_AUDIO]:
                fp3 = os.path.join(HIST, "%s.a%s.%s" % (rid, a.get("n"), a.get("ext") or "mp3"))
                if os.path.exists(fp3):
                    rec_audio_blobs.append((open(fp3, "rb").read(), a.get("ext") or "mp3"))
        except Exception:
            pass
    # 空白 prompt 不能送。external_prompt_overwrite 是空的時候 Director 會退回模板裡存的
    # simple_prompt——那是上一次在 ComfyUI 跑過的舊劇情（實查模板裡還帶著別的 LoRA
    # 觸發詞），影片照樣算出來，內容卻跟這張卡毫無關係，而且沒有任何地方會說。
    if not full_prompt.strip():
        raise SubmitError("這筆沒有 prompt 可以送（內容是空的）", 400)
    _wf = body.get("wf") if isinstance(body.get("wf"), dict) else {}
    full_prompt, lora_hits = lora_prompt_apply(full_prompt, run_mode, _wf.get("lora_prompt"))
    if lora_hits:
        slog("  [lora] 觸發詞插入 %d 處：%s" % (lora_hits, str(_wf.get("lora_prompt"))[:80]))
    if blob is None:
        blob, ext2 = parse_data_image(body.get("image"))
        if blob is not None:
            ext = ext2
    if blob is None:
        # 換一台電腦開同一份草稿時，瀏覽器裡沒有編碼過的 base64，
        # 只剩草稿裡的上傳庫雜湊。上傳庫存的就是當初 regUpload 送上來的
        # 同一份原圖，取出來的位元組完全一樣。
        blob, ext3 = upload_blob(body.get("image_hash"))
        if blob is not None:
            ext = ext3
    more_blobs = []
    for durl in (body.get("more") or [])[:8]:
        mb, _ = parse_data_image(durl)
        if mb:
            more_blobs.append(mb)
    if not more_blobs:
        for h in (body.get("more_hashes") or [])[:8]:
            mb, _ = upload_blob(h)
            if mb:
                more_blobs.append(mb)
    extra_blobs.extend(more_blobs)
    # REF2VA 的參考音訊：節點限 3 個、且必須有圖或影片作伴，總長 15 秒（長度在前端擋）
    audio_blobs = []
    for durl in (body.get("audios") or [])[:MAX_REF_AUDIO]:
        ab, aext = parse_data_audio(durl)
        if ab:
            audio_blobs.append((ab, aext))
    if not audio_blobs:
        audio_blobs = rec_audio_blobs
    if audio_blobs and run_mode != "ref2va":
        raise SubmitError("只有 REF2VA 支援參考音訊", 400)
    if blob is None and run_mode != "t2va":
        raise SubmitError("沒有可用的圖片（rec_id、image、上傳庫雜湊都取不到圖）", 400)
    dur = None
    md = re.search(r"\d+", str(body.get("dur") or ""))
    if md:
        dur = int(md.group(0))
    # 整批上傳共用 120 秒；不設的話 12 個檔各等 60 秒就是 720 秒，
    # 而前端給這條路線的預算是 240 秒。
    COMFY_UP.deadline = time.time() + 120
    try:
        up_name = None
        if blob is not None:
            name = "h3webui_%s.%s" % (new_id(), ext)
            up_name = comfy_upload(name, blob).get("name", name)
        extra_names = []
        for k2, mb in enumerate(extra_blobs):
            nm2 = "h3webui_%s_x%d.jpg" % (new_id(), k2 + 1)
            extra_names.append(comfy_upload(nm2, mb).get("name", nm2))
        audio_names = []
        for k3, (ab, aext) in enumerate(audio_blobs):
            nm3 = "h3webui_%s_a%d.%s" % (new_id(), k3 + 1, aext)
            audio_names.append(comfy_upload(nm3, ab, "audio/" + aext).get("name", nm3))
        if audio_names and not (up_name or extra_names):
            raise SubmitError("參考音訊必須搭配至少一張參考圖", 400)
        if len(audio_names) + len(extra_names) + (1 if up_name else 0) > MAX_REF_FILES:
            raise SubmitError("參考素材最多 %d 個（圖＋音訊合計）" % MAX_REF_FILES, 400)
        flds = split_fields(full_prompt, run_mode)
        # 插過觸發詞之後，前端帶來的三欄還是插入前的版本；改用最終文字重拆，
        # 拖回 ComfyUI 時 builder 介面看到的才跟實際送出的一致
        _fld = (lambda k: flds[k]) if lora_hits else (lambda k: str(body.get(k) or flds[k]))
        graph, extra = comfy_build(_fld("imd"),
                                   _fld("soundscape"),
                                   _fld("music"),
                                   up_name, dur,
                                   body.get("wf"), blob,
                                   mode=run_mode, extra_names=extra_names, full_prompt=full_prompt,
                                   audio_names=audio_names)
        payload = {"prompt": graph, "client_id": WS_CLIENT_ID}
        if body.get("prompt_id"):
            payload["prompt_id"] = str(body["prompt_id"])
        if extra:
            payload["extra_data"] = extra
        r = comfy_api("/prompt", payload, timeout=60)
    except SubmitError:
        raise
    except Exception as e:
        detail = ""
        if hasattr(e, "read"):
            try: detail = e.read().decode("utf-8", "replace")[:500]
            except Exception: pass
        raise SubmitError("送出失敗: %s %s" % (e, detail), 502)
    finally:
        # keep-alive 下同一條執行緒會處理下一個請求，殘留的截止時間
        # 會被它繼承，下一次上傳會莫名其妙地馬上逾時。
        COMFY_UP.deadline = 0.0
    if "prompt_id" not in r:
        raise SubmitError("ComfyUI 拒收: %s" % json.dumps(r, ensure_ascii=False)[:500], 502)
    # ComfyUI 對每個輸出節點分開驗證：只要還有一個過關就回 200，被判掉的只寫在 node_errors。
    # 影片輸出節點被丟掉時，圖照樣「跑完」且回報 success，卻什麼都沒產出——不在這裡擋下來，
    # 就會變成任務清單上兩秒完成、然後找不到影片的鬼故事。
    ne = r.get("node_errors") or {}
    if ne:
        try:
            comfy_api("/queue", {"delete": [r["prompt_id"]]}, timeout=15)
        except Exception:
            pass
        raise SubmitError("ComfyUI 判掉了工作流裡的輸出節點，這張圖不會產出影片："
                          + chr(10) + _node_errors_text(ne), 400)
    return r["prompt_id"]



# ---------------------------------------------------------------- queue worker
# 單一派工執行緒：ComfyUI 佇列深度固定維持 1，由我們自己排隊。這樣所有瀏覽器看到的
# 順序一致，而且 GPU 一次只跑一支（llama 與 ComfyUI 的交接仍走既有的協調邏輯）。
QWAKE = threading.Event()


def hist_note_recipe(rid, recipe):
    """把這次送單實際用的配方記回歷史紀錄。紀錄是生成 prompt 的那一刻存的，
    配方常常是之後才在卡片上選；不補的話，從歷史重送會用到別組 LoRA 與觸發詞。"""
    if not (rid and ID_RE.match(str(rid))) or recipe is None:
        return
    fp = os.path.join(HIST, str(rid) + ".json")
    r = str(recipe)[:40]
    with LOCK:
        try:
            with open(fp, encoding="utf-8") as f:
                rec = json.load(f)
            if rec.get("recipe", "") == r:
                return
            rec["recipe"] = r
            tmp = fp + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=1)
            os.replace(tmp, fp)
        except Exception:
            pass


def qenqueue(payload, mode="", title="", rec_id="", idem=""):
    """建立一筆 queued 任務並喚醒派工。回傳新列。"""
    with QLOCK:
        if idem:
            for r in QROWS.values():
                if r.get("idem") == idem:
                    return dict(r)          # 同一次點擊重送：回傳既有列，不重複算
        jid = new_id()
        # 秒級精度不夠：批次送出時好幾筆會落在同一秒，排序就變成隨機。
        # 存浮點秒，前端才排得出真正的先後。
        row = {"id": jid, "ts": round(time.time(), 3), "state": "queued",
               "mode": mode or str(payload.get("mode") or ""), "title": title[:120],
               "rec_id": rec_id, "idem": idem, "prompt_id": "",
               "payload": payload, "video": "", "first_frame": "", "error": "",
               "t_submit": 0, "t_end": 0}
        qsave(row)
    QWAKE.set()
    if isinstance(payload, dict) and "recipe" in payload:      # QLOCK 已放開才碰 LOCK
        hist_note_recipe(rec_id, payload.get("recipe"))
    return dict(row)


def _qoutputs(rec):
    """從 ComfyUI 的 history 紀錄挑出成品影片與首尾幀。"""
    vid = first = last = ""
    for _nid, o in (rec.get("outputs") or {}).items():
        for _k, v in o.items():
            if not isinstance(v, list):
                continue
            for it in v:
                if not isinstance(it, dict) or "filename" not in it:
                    continue
                rel = ((it.get("subfolder", "") + "/" + it["filename"]).lstrip("/")).replace("\\", "/")
                low = rel.lower()
                if low.endswith(VIDEO_EXT):
                    vid = rel
                elif low.endswith("-first-frame.png"):
                    first = rel
                elif low.endswith("-last-frame.png"):
                    last = rel
    return vid, first, last


def qreconcile(row):
    """依 prompt_id 向 ComfyUI 問結果，更新任務狀態。回傳 True 表示已結束。"""
    pid = row.get("prompt_id")
    if not pid:
        return False
    try:
        hist = comfy_api("/history/" + pid, timeout=20)
    except Exception:
        return False
    rec = (hist or {}).get(pid)
    if not rec:
        # 還沒進 history：可能在跑，也可能 ComfyUI 重啟把它弄丟了
        try:
            q = comfy_api("/queue", timeout=15)
            alive = any(str(pid) in json.dumps(x) for x in
                        (q.get("queue_running") or []) + (q.get("queue_pending") or []))
        except Exception:
            alive = True
        if not alive:
            qset(row["id"], state="error", error="ComfyUI 已無此任務（可能重啟過）",
                 t_end=int(time.time()))
            return True
        return False
    st = rec.get("status", {}) or {}
    err = ""
    for msg in st.get("messages", []):
        if msg and msg[0] == "execution_error":
            err = str((msg[1] or {}).get("exception_message", ""))[:400]
    if st.get("completed"):
        vid, first, last = _qoutputs(rec)
        qset(row["id"], state="done" if vid else "error",
             video=vid, first_frame=first,
             error="" if vid else (err or "ComfyUI 回報完成但沒有輸出影片"),
             t_end=int(time.time()))
        if vid:
            media_prefetch(vid, first, last)
        return True
    if st.get("status_str") == "error" or err:
        qset(row["id"], state="error", error=err or "ComfyUI 執行失敗", t_end=int(time.time()))
        return True
    return False


def qworker():
    """派工迴圈：一次只讓一支在 ComfyUI 跑，跑完才送下一支。"""
    while True:
        try:
            with QLOCK:
                running = [dict(r) for r in QROWS.values() if r.get("state") == "running"]
                queued = sorted([dict(r) for r in QROWS.values() if r.get("state") == "queued"],
                                key=lambda r: r.get("ts") or 0)
            for r in running:
                qreconcile(r)
            with QLOCK:
                busy = any(r.get("state") in ("running", "submitting") for r in QROWS.values())
            if queued and not busy:
                row = queued[0]
                qset(row["id"], state="submitting")
                try:
                    pid = comfy_submit(row["payload"])
                    qset(row["id"], state="running", prompt_id=pid, t_submit=int(time.time()))
                except SubmitError as e:
                    qset(row["id"], state="error", error=e.msg, t_end=int(time.time()))
                except Exception as e:
                    qset(row["id"], state="error", error=str(e)[:300], t_end=int(time.time()))
        except Exception as e:
            sys.stderr.write("  queue worker: " + str(e)[:200] + chr(10))
        QWAKE.wait(3.0)
        QWAKE.clear()



def gpu_prepare_step(target):
    """llama 與 ComfyUI 在同一張卡時的交接判斷。ready=False 表示要等一下再問。"""
    thr = int(CONFIG.get("gpu_free_mb", 4000))
    mem = gpu_mem()
    used = mem.get("used_mb")
    if used is None:
        return ({"ready": True, "state": "no_gpu_info", "gpu": mem})
    in_flight = LLAMA_INFLIGHT[0] > 0
    since_llama = (time.time() - LLAMA_LAST[0]) if LLAMA_LAST[0] else 1e9
    # 在別台機器上的那一方不碰這張卡。本機 VRAM 降不降下來跟它無關，
    # 等下去只是無端不能用。cut_gpu_block 早就這樣判了，這裡漏掉。
    comfy_here, llama_here = cut_rivals_local()
    if target == "llama":
        if not llama_here:
            return ({"ready": True, "state": "llama_remote", "gpu": mem})
        if in_flight:
            # 另一個 llama 呼叫正在跑：同對象直接放行
            return ({"ready": True, "state": "llama_resident", "gpu": mem})
        if since_llama < 8 and not comfy_busy():
            # 權重還在卡上（idle 卸載前）且 ComfyUI 沒在算圖：連續呼叫不用等
            return ({"ready": True, "state": "llama_resident", "gpu": mem})
        if used <= thr:
            return ({"ready": True, "state": "gpu_free", "gpu": mem})
        busy = comfy_busy()
        if busy:
            return ({"ready": False, "state": "comfy_busy", "gpu": mem})
        # 去背服務也押著 VRAM（BiRefNet 約 4.4GB），而 comfy_free 卸不到它。
        # 不叫它的話用量永遠降不到門檻以下，這裡會空轉到逾時。
        freed = []
        if cut_unload() is not None:
            freed.append("cutout")
        try:
            comfy_free()
            freed.append("comfy")
        except Exception as e:
            freed.append("comfy失敗(%s)" % str(e)[:40])
        state = "freeing_" + "+".join(freed) if freed else "nothing_to_free"
        time.sleep(1.5)
        mem = gpu_mem()
        return ({"ready": (mem.get("used_mb") or 0) <= thr, "state": state, "gpu": mem})
    if target == "comfy":
        if not comfy_here:
            return ({"ready": True, "state": "comfy_remote", "gpu": mem})
        if in_flight and llama_here:
            return ({"ready": False, "state": "llama_in_use", "gpu": mem})
        if used <= thr:
            return ({"ready": True, "state": "gpu_free", "gpu": mem})
        if comfy_busy():
            # 高 VRAM 是 ComfyUI 自己在用：直接排進它的佇列即可
            return ({"ready": True, "state": "comfy_owns_gpu", "gpu": mem})
        if since_llama < 90 and llama_here:
            return ({"ready": False, "state": "waiting_llama_unload", "gpu": mem})
        # 沒人聲稱佔用：多半是 ComfyUI 閒置駐留的模型，送單無妨
        return ({"ready": True, "state": "assume_comfy_resident", "gpu": mem})
    return {"error": "target 必須是 llama 或 comfy"}


def comfy_state(pid):
    """問 ComfyUI 這個工作現在怎樣。連不上會丟例外（那不等於工作失敗）。"""
    hist = comfy_api("/history/" + pid, timeout=15)
    if pid in hist:
        rec = hist[pid]
        st = rec.get("status", {})
        outs = []
        for nid, o in (rec.get("outputs") or {}).items():
            for k, v in o.items():
                if isinstance(v, list):
                    for it in v:
                        if isinstance(it, dict) and "filename" in it:
                            outs.append((it.get("subfolder", "") + "/" + it["filename"]).lstrip("/"))
        err = ""
        for msg in st.get("messages", []):
            if msg[0] == "execution_error":
                err = str(msg[1].get("exception_message", ""))[:400]
            elif msg[0] == "execution_interrupted" and not err:
                err = "工作被中斷（有人在 ComfyUI 或任務清單按了取消）"
        state = "done" if st.get("completed") else ("error" if st.get("status_str") == "error" else "running")
        comfy_note(pid, state, (outs[-1] if (state == "done" and outs) else (err[:80] if err else "")))
        if state == "done" and outs:
            # 不走任務清單的工作（Movie、I2VA Continue）也要先鏡像，合併時才不用一支一支等
            media_prefetch(*outs)
        return {"state": state, "outputs": outs, "error": err}
    q = comfy_api("/queue", timeout=15)
    for item in q.get("queue_running", []):
        if len(item) > 1 and item[1] == pid:
            comfy_note(pid, "running")
            return {"state": "running"}
    for i, item in enumerate(q.get("queue_pending", [])):
        if len(item) > 1 and item[1] == pid:
            comfy_note(pid, "queued", "(pos %d)" % (i + 1))
            return {"state": "queued", "pos": i + 1}
    comfy_note(pid, "unknown")
    return {"state": "unknown"}


# ---------------------------------------------------------------- I2VA Continue：背景接龍
# 一條鏈要算一兩個小時，不能靠瀏覽器開著推進：分頁一關，下一章就沒有人送了。
# 這裡只負責「照順序跑」。prompt 怎麼組（官方 skill、指南、經驗庫、配方）還是網頁按下開始時
# 算好送來的，存成 <id>.run.json：那套邏輯只留一份，h3-server 重啟也接得回去。
IC_LOCK = threading.RLock()
IC_Q = []                        # 等著跑的專案 id
IC_CUR = {"id": "", "ch": -1}    # 正在跑的專案與章節
IC_STAT = {}                     # rid -> 進度文字（只在記憶體，變化太頻繁不寫檔）
IC_PLANNING = set()
IC_CANCEL = {}                   # rid -> "stopping"（取消送出中）/ "stopped"
IC_STOP_AFTER = [False]          # 這一章做完就全部停
IC_WAKE = threading.Event()


class IcStopped(Exception):
    pass


class IcPaused(Exception):
    pass


def ic_active(rid):
    with IC_LOCK:
        return rid in IC_Q or rid in IC_PLANNING or IC_CUR["id"] == rid


def ic_stat(rid, text):
    with IC_LOCK:
        IC_STAT[rid] = text


def ic_check_stop(rid):
    with IC_LOCK:
        if IC_CANCEL.get(rid) == "stopped":
            raise IcStopped()


def ic_nap(rid, secs):
    """睡的時候也要能馬上被停止叫醒。"""
    t1 = time.time() + secs
    while time.time() < t1:
        ic_check_stop(rid)
        time.sleep(min(0.4, max(0.01, t1 - time.time())))


def ic_gpu_wait(rid, target, i):
    label = {"comfy_busy": "等待 ComfyUI 生成完成", "waiting_llama_unload": "等待 llama 閒置卸載",
             "llama_in_use": "llama 正在生成中，等它完成"}
    for _n in range(400):             # 最多等 ~20 分鐘；超時放行，跟網頁的 gpuPrepare 一樣
        try:
            d = gpu_prepare_step(target)
        except Exception:
            return
        if d.get("ready") or d.get("error") or d.get("state") == "no_gpu_info":
            return
        ic_stat(rid, "第 %d 章：GPU 協調：%s…" % (i + 1, label.get(d.get("state"), "釋放 VRAM 中")))
        ic_nap(rid, 3)


def ic_llm(messages, temp, max_tokens, top_p=0.95, rid=None):
    """呼叫 llama-server。接龍沒有人看著，暫時性的錯誤自己重試幾次，不要整條鏈停在這裡。"""
    body = {"model": "qwen3.8-27b", "messages": messages, "stream": False, "temperature": temp,
            "top_p": top_p, "top_k": 20, "seed": int.from_bytes(os.urandom(4), "big") & 0x7fffffff,
            "max_tokens": max_tokens, "chat_template_kwargs": {"enable_thinking": False}}
    maybe_reload_config()
    with QLOCK:
        LLAMA_INFLIGHT[0] += 1
    try:
        err = None
        for a in range(3):
            try:
                j = llama_api("/v1/chat/completions", body, timeout=280)
                if j.get("choices"):
                    return j
                err = str(j.get("error") or j)[:200]
            except Exception as e:
                err = str(e)[:200]
            if rid:
                ic_nap(rid, 8)
            else:
                time.sleep(8)
        raise IOError("llama-server: %s" % err)
    finally:
        with QLOCK:
            LLAMA_INFLIGHT[0] = max(0, LLAMA_INFLIGHT[0] - 1)
        LLAMA_LAST[0] = time.time()


def ic_llm_image(raw):
    """給 LLM 看的圖：長邊 1024 的 JPEG，跟網頁 encode() 一樣。最後一幀是放大過的 PNG，整張送太大。"""
    ff = find_ffmpeg()
    if ff:
        try:
            r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-frames:v", "1",
                                "-vf", "scale='if(gt(iw,ih),min(1024,iw),-2)':'if(gt(iw,ih),-2,min(1024,ih))'",
                                "-q:v", "3", "-f", "mjpeg", "pipe:1"],
                               input=raw, capture_output=True, timeout=45)
            if r.returncode == 0 and r.stdout:
                return "data:image/jpeg;base64," + base64.b64encode(r.stdout).decode()
        except Exception as e:
            slog("  [continue] 縮圖失敗，送原圖：%s" % str(e)[:120])
    mime = "image/png" if raw[:4] == b"\x89PNG" else "image/jpeg"
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode())


def ic_first_frame(rid, rec, i):
    """這一章的第一幀。絕不退回原圖：接不上就是接不上，悄悄換一張會得到一支斷掉的長片。"""
    if i == 0:
        import glob as _g
        hits = _g.glob(os.path.join(CONTINUES, rid + ".img.*"))
        if not hits:
            raise IOError("找不到這個專案的起始圖")
        with open(hits[0], "rb") as f:
            return f.read()
    prev = rec["chapters"][i - 1]["video"]
    if not prev:
        raise IOError("第 %d 章還沒有影片" % i)
    # 改接的畫面只有這台有；鏡像資料夾可以刪、移機不用帶，不見了就從影片再抽一次（同一幀）
    lf, cut = rec["chapters"][i - 1].get("last_frame"), rec["chapters"][i - 1].get("cut")
    if lf and cut and not os.path.isfile(media_path(lf) or ""):
        ff, vp = find_ffmpeg(), media_fetch(prev.replace("\\", "/"))
        got = ic_frame_at(ff, vp, cut) if ff and vp else b""
        if got:
            with open(media_path(lf), "wb") as f:
                f.write(got)
            return got
    rel = rec["chapters"][i - 1].get("last_frame") \
        or re.sub(r"\.(mp4|webm|mkv|mov)$", "-last-frame.png", prev.replace("\\", "/"), flags=re.I)
    for a in range(6):
        p0 = media_fetch(rel)
        if p0:
            with open(p0, "rb") as f:
                return f.read()
        _media_fetch_fail.pop(media_path(rel), None)   # 失敗會記 60 秒，不清掉重試就沒有意義
        ic_stat(rid, "第 %d 章：等待上一章的最後一幀…" % (i + 1))
        ic_nap(rid, 15)
    raise IOError("讀不到第 %d 章的最後一幀" % i)


def ic_trim(c):
    # 模型偶爾不吐 EOS 會把欄位再寫一遍，這裡切掉
    m0 = re.search(r"non_diegetic_music:?", c)
    if not m0:
        return c.strip()
    head, tail = c[:m0.end()], c[m0.end():]
    m = re.search(r"\n\s*\n(?=\s*(?:integrated_multimodal_description|subject_definitions))", tail)
    if m:
        tail = tail[:m.start()]
    return (head + tail).strip()


def ic_repair(c):
    # 跟網頁 repair() 同一套：剝掉 Markdown 與第一個合法起點之前的雜訊
    c = re.sub(r"^#{1,6}\s.*$", "", c, flags=re.M)
    c = re.sub(r"\*\*([A-Za-z_ ]+)\*\*\s*(:)?", lambda m: m.group(1) + (m.group(2) or ""), c)
    c = re.sub(r"^\s*[-*]\s+(?=[a-z_]+\s*:)", "", c, flags=re.M)
    c = re.sub(r"^\s*[\r\n]+", "", c)
    hits = [c.find(x) for x in ("For the target video", "How the reference pictures align",
                                "subject_definitions", "integrated_multimodal_description") if c.find(x) >= 0]
    if hits and min(hits) > 0:
        c = c[min(hits):]
    c = re.sub(r"^(integrated_multimodal_description|overall_soundscape|non_diegetic_music)\s*$", r"\1:", c, flags=re.M)
    return c.strip()


def ic_field(c, a, b):
    i = c.find(a)
    if i < 0:
        return ""
    j = c.find(b) if b else -1
    return c[i + len(a):j if j > 0 else None].strip()


def ic_speaker_issues(c):
    out, order = [], []
    for seg in re.split(r"(?=\[Shot\s*\d+\])", c):
        if "<d>" not in seg:
            continue
        ms = re.search(r"\[Shot\s*(\d+)\]", seg)
        for m in re.finditer(r"<d>", seg):
            ids = list(re.finditer(r"\(S(\d)(?:\s*,\s*S?\d)*\)", seg[:m.start()]))
            if ids:
                order.append(int(ids[-1].group(1)))
            else:
                out.append("[Shot %s] 有台詞卻沒有標說話者 (S#)" % (ms.group(1) if ms else "?"))
    first = []
    for x in order:
        if x not in first:
            first.append(x)
    for k, x in enumerate(first):
        if x != k + 1:
            out.append("說話者編號沒有照發聲順序：第 %d 個開口的被標成 (S%d)，應該是 (S%d)" % (k + 1, x, k + 1))
            break
    return list(dict.fromkeys(out))


def ic_validate(c, seg, i, n, dialogue):
    """跟網頁 validate() + 接龍的檢查同一套規則。E 會帶著問題清單重寫，W 只提醒。"""
    E, W = [], []
    if "<think>" in c:
        E.append("<think> 洩漏進 content")
    if re.search(r"^\s*```", c, re.M):
        E.append("輸出含程式碼框")
    for l in ("integrated_multimodal_description", "overall_soundscape", "non_diegetic_music"):
        if l not in c:
            E.append("缺欄位 " + l)
    W.extend(ic_speaker_issues(c))
    m = re.search(r"integrated_multimodal_description:?\s*([\s\S]*?)\s*overall_soundscape", c)
    body = m.group(1) if m else c
    dall = re.findall(r"<d>[\s\S]*?</d>", c)
    try:
        for l in load_lessons():
            if not l.get("enabled") or not l.get("ban") or l.get("mode") not in ("all", "both", "i2va"):
                continue
            for b in re.split(r"[,，;；]", l["ban"]):
                b = b.strip()
                if b and b.lower() in c.lower():
                    E.append("經驗庫禁用詞出現：「%s」" % b)
    except Exception:
        pass
    words = len(body.split())
    req = load_required() or {}
    if req.get("min_words", 0) > 0 and words < req["min_words"]:
        E.append("主段落 %d 詞 < 門檻 %d" % (words, req["min_words"]))
    for d in dall:
        if req.get("lang_lock") == "japanese" and not re.search(r"[\u3040-\u30ff]", d):
            E.append("對白缺假名（要求日文）：「%s」" % re.sub(r"<[^>]+>", "", d)[:20])
        t = re.sub(r"</?d>|\[[A-Za-z]+\]", "", d).strip()
        cnt = len(re.sub(r"\s", "", t)) if re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", t) else len(t.split())
        if req.get("line_min", 0) > 0 and cnt < req["line_min"]:
            E.append("台詞太短（%d < %d）：「%s」" % (cnt, req["line_min"], t[:20]))
        if req.get("line_max", 0) > 0 and cnt > req["line_max"]:
            W.append("台詞太長（%d > %d）" % (cnt, req["line_max"]))
    shots = len(re.findall(r"\[Shot\s*\d+\]", body, re.I))
    if shots > 1:
        E.append("只能有一個鏡頭，這裡寫了 %d 個 [Shot]" % shots)
    need = int(round(seg * 17))
    if words < need:
        E.append("主段落只有 %d 字，%d 秒至少要 %d 字，動作要填滿整段" % (words, seg, need))
    if dialogue and "<d>" not in c:
        E.append("本章有台詞計畫，但沒有寫進 <d>")
    # 實測 16 章裡有 2 章寫成 "the shot ends as she is holding the staff out"：
    # 結尾是靜止的，下一章就得從不動的姿勢重新起步，接點會頓一下。
    tail = re.sub(r"<d>[\s\S]*?</d>", "<d>", body)[-420:]
    if i < n - 1:
        end = re.search(r"the shot ends as ([^.!?]*)", tail, re.I)
        if not end:
            E.append("結尾要寫成「The shot ends as ＜主體＞ is ＜正在進行的動作＞」")
        elif re.search(r"\b(?:is|are)\s+(?:still\s+)?(?:holding|standing(?! up)|looking|gazing|staring|smiling|waiting|sitting(?! (?:up|down))|keeping|remaining)\b", end.group(1), re.I):
            E.append("結尾是靜止的姿勢，要改成動作進行到一半的瞬間（正在轉身、邁步、伸手…）")
    elif re.search(r"\b(?:freez\w*|motionless|hold(?:s|ing)? (?:this|that|the) (?:final |same )?(?:state|pose|position|stance)|until the end of the clip)\b", tail, re.I):
        E.append("最後一章也要動到最後一秒，不要寫定格或維持姿勢")
    said = re.sub(r"</?d>|\[[A-Za-z]+\]", " ", " ".join(dall))
    need2 = len(re.findall(r"[\u3040-\u30ff]", said)) / 7.0 + len(re.findall(r"[\u4e00-\u9fff]", said)) / 4.5 \
        + len(re.findall(r"[A-Za-z']+", said)) / 2.5
    if need2 > seg:
        W.append("台詞大約要講 %.1f 秒，超過這一章的 %d 秒" % (need2, seg))
    return E, W


def ic_hard(E):
    # 字數／漏台詞是品質問題：修正重試用完還是不夠就照送。少了欄位或多個鏡頭才是壞掉，要停。
    return any(re.search(r"缺欄位|程式碼框|<think>|一個鏡頭", e) for e in E)


def ic_gen_prompt(rid, i, img_url, spec, rec):
    cs = spec["chapters"][i]
    n, seg = len(rec["chapters"]), rec["seg"]
    dialogue = rec["chapters"][i]["dialogue"]
    cfg = spec.get("retry") or {}
    mx, lvl = _i(cfg.get("max"), 0, 20, 3), str(cfg.get("lvl") or "noerr")
    best, prev = None, None
    for a in range(mx + 1):
        ic_check_stop(rid)
        ic_stat(rid, "第 %d/%d 章：寫 prompt 中…%s" % (i + 1, n, ("（重試 %d/%d）" % (a, mx)) if a else ""))
        msgs = [{"role": "system", "content": str(spec.get("sys") or "") + str(cs.get("extra") or "")},
                {"role": "user", "content": [{"type": "text", "text": str(cs.get("utext") or "")},
                                             {"type": "image_url", "image_url": {"url": img_url}}]}]
        if prev and prev["issues"]:
            msgs.append({"role": "assistant", "content": prev["content"]})
            msgs.append({"role": "user", "content": "你的上一版輸出沒有通過檢查：\n" + "\n".join(prev["issues"])
                         + "\n\n重新輸出**完整**的 prompt（所有欄位都要），只修正上面列的問題，其他內容保持不變。只輸出修正後的 prompt，不要任何說明。"})
        j = ic_llm(msgs, 0.7, 4000, rid=rid)
        content = ic_repair(ic_trim(j["choices"][0]["message"].get("content") or ""))
        E, W = ic_validate(content, seg, i, n, dialogue)
        cur = {"content": content, "E": E, "W": W, "score": len(E) * 100 + len(W), "attempts": a + 1}
        prev = {"content": content, "issues": ["[錯誤] " + e for e in E] + (["[警告] " + w for w in W] if lvl == "ok" else [])}
        if best is None or cur["score"] < best["score"]:
            best = cur
        if not E and (lvl == "noerr" or not W):
            best = cur
            break
    best["attempts"] = a + 1
    return best


def ic_write(rid, i, raw, spec):
    ic_ch(rid, i, vstate="writing", error="")
    ic_gpu_wait(rid, "llama", i)
    rec = ic_load(rid)
    best = ic_gen_prompt(rid, i, ic_llm_image(raw), spec, rec)
    zh = ""
    ic_check_stop(rid)
    if spec.get("tr") and spec.get("tr_sys"):
        try:
            t = ic_llm([{"role": "system", "content": spec["tr_sys"]}, {"role": "user", "content": best["content"]}],
                       0.3, 1800, top_p=0.9, rid=rid)
            zh = (t["choices"][0]["message"].get("content") or "").strip()
        except IcStopped:
            raise
        except Exception as e:
            zh = "（翻譯失敗：%s）" % str(e)[:100]
    hard = ic_hard(best["E"])
    # hard 另外存：下次按繼續要重寫，不能把壞的 prompt 送出去
    ic_ch(rid, i, content=best["content"], zh=zh, v_e=best["E"], v_w=best["W"], attempts=best["attempts"],
          dur_gen=rec["seg"], edited=False, hard=hard, stale=hard, vstate="",
          state="fail" if best["E"] else ("warn" if best["W"] else "ok"))


def ic_wait_job(rid, i, pid, resuming):
    """等 ComfyUI 算完，回傳影片路徑；工作不見了回 None。"""
    n = len(ic_load(rid)["chapters"])
    t0, lost, down = time.time(), 0, 0
    while time.time() - t0 < 8 * 3600:      # ComfyUI 可能還在排別的工作，上限放很寬
        ic_check_stop(rid)
        try:
            st = comfy_state(pid)
        except Exception:
            # 連不上不等於工作失敗，它很可能還在算。pid 留著，恢復後收得回來
            down += 1
            ic_stat(rid, "第 %d/%d 章：ComfyUI 連不上，等它恢復…（%d）" % (i + 1, n, down))
            if down >= 6 and IC_STOP_AFTER[0]:
                raise IcPaused()
            ic_nap(rid, 5)
            continue
        down = 0
        if st["state"] == "error":
            with IC_LOCK:
                mine = rid in IC_CANCEL      # 我們自己中斷的，不是失敗
            if mine:
                raise IcStopped()
            if resuming:
                return None
            ic_ch(rid, i, pid="")
            raise IOError("ComfyUI 生成失敗：" + (st.get("error") or "原因不明"))
        if st["state"] == "done":
            v = next((o for o in st.get("outputs", []) if o.lower().endswith(VIDEO_EXT)), None)
            if not v:
                if resuming:
                    return None
                ic_ch(rid, i, pid="")
                raise IOError("完成了但找不到輸出的影片")
            return v
        # unknown＝佇列與歷史都沒有，ComfyUI 重啟過才會這樣；連續幾次才算數
        lost = lost + 1 if st["state"] == "unknown" else 0
        if lost >= 12:
            return None
        what = "排隊中（第 %s 位）" % st.get("pos") if st["state"] == "queued" else "影片生成中"
        ic_stat(rid, "第 %d/%d 章：%s（%d 分鐘）" % (i + 1, n, what, (time.time() - t0) / 60))
        ic_nap(rid, 5)
    raise IOError("等待影片逾時（8 小時）")


def ic_render(rid, i, raw, spec):
    ic_ch(rid, i, vstate="rendering", error="")
    rec = ic_load(rid)
    s = rec["chapters"][i]
    video = None
    if s["pid"]:
        video = ic_wait_job(rid, i, s["pid"], True)      # 重啟前送出的，先看還在不在
        if not video:
            ic_ch(rid, i, pid="")
    if not video:
        ic_check_stop(rid)
        ic_gpu_wait(rid, "comfy", i)
        ic_stat(rid, "第 %d/%d 章：送出影片生成…" % (i + 1, len(rec["chapters"])))
        ic_check_stop(rid)
        cs = spec["chapters"][i]
        wf = dict(cs.get("wf") or {})
        # 第 2 章起圖是上一章的輸出。放大過的尺寸重新貼齊畫布時，少數比例會差一格，
        # 畫面大小不同，合併（-c copy）就壞了。把比例明確帶上，每章的畫布才一樣。
        if i > 0 and not wf.get("aspect") and raw[:4] == b"\x89PNG" and len(raw) > 24:
            wf["aspect"] = "%d:%d" % struct.unpack(">II", raw[16:24])
        rec = ic_load(rid)
        content = rec["chapters"][i]["content"]
        mime = "image/png" if raw[:4] == b"\x89PNG" else ("image/webp" if raw[8:12] == b"WEBP" else "image/jpeg")
        na = lambda t: "N/A" if re.match(r"^n/a\.?$", t.strip(), re.I) else t
        # prompt_id 由這邊先決定並先存檔：對方收下了、回應卻斷在路上的話，
        # 下次還認得出這一支，不會變成沒人管的工作再重送一支。
        pid = str(uuid.uuid4())
        ic_ch(rid, i, pid=pid, snap=ic_snap(cs.get("snap")), sent=content,
              parent=rec["chapters"][i - 1]["video"] if i > 0 else "")
        try:
            comfy_submit({"prompt_id": pid, "image": "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode()), "mode": "i2va",
                            "content": content, "dur": "%d seconds" % rec["seg"], "wf": wf,
                          "imd": ic_field(content, "integrated_multimodal_description:", "overall_soundscape:"),
                          "soundscape": na(ic_field(content, "overall_soundscape:", "non_diegetic_music:")),
                          "music": na(ic_field(content, "non_diegetic_music:", None))})
        except SubmitError as e:
            if e.code < 500:              # 對方明確拒收，這個 id 不會存在
                ic_ch(rid, i, pid="")
            raise
        # 上傳途中按了停止：那時還沒有 pid 可以取消，要在這裡補，否則那支會沒人管地算完
        with IC_LOCK:
            stopping = rid in IC_CANCEL
        if stopping:
            try:
                comfy_api("/queue", {"delete": [pid]}, timeout=10)
                comfy_api("/interrupt", {"prompt_id": pid}, timeout=10)
            except Exception:
                pass
            raise IcStopped()
        video = ic_wait_job(rid, i, pid, False)
        if not video:
            raise IOError("ComfyUI 找不到這個工作（可能重啟過），按「繼續生成」會重新送出")
    ic_ch(rid, i, video=video, pid="", vstate="", error="")


IC_FACE_RETRIES = 2              # 往前找不到有臉的畫面時，這一章最多重算幾次
IC_FACE_SYS = """You inspect one frame from a video. The next clip will be generated from this frame alone, so the main character's face must be recognizable in it.

Answer "visible" only if the eyes, nose and mouth area of the main character can be seen: frontal, three-quarter or side profile all count, even if looking down.
Answer "not_visible" if the head is outside the frame or cut off above the eyes, if the character is turned away so only the back of the head shows, or if a hand, hair or object covers most of the face.
Answer "no_person" only if nobody is in the frame.

Output only JSON: {"face":"visible|not_visible|no_person","view":"front|three_quarter|profile|back|out_of_frame|covered"}"""
IC_FACE_FAIL = "第 %d 章重算了還是看不到臉，已暫停。可以改 prompt 後重新生成，或按「仍用這一幀繼續」"


def ic_face_ok(rid, raw, identity):
    """False＝確定看不到臉。判斷不出來時回 None：LLM 暫時出錯不該把整條鏈卡住。"""
    try:
        j = ic_llm([{"role": "system", "content": IC_FACE_SYS},
                    {"role": "user", "content": [{"type": "text", "text": "Main character: %s\nCheck this frame." % (identity or "the main person")},
                                                 {"type": "image_url", "image_url": {"url": ic_llm_image(raw)}}]}],
                   0, 60, rid=rid)
        txt = j["choices"][0]["message"].get("content") or ""
        o = json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
    except IcStopped:
        raise
    except Exception as e:
        slog("  [continue] 判斷臉部失敗，放行：%s" % str(e)[:120])
        return None
    # 角色整個走出畫面也是「下一章看不到臉」。只有規劃裡根本沒有人物時，空景才算通過
    if o.get("face") == "no_person":
        return not identity
    return o.get("face") != "not_visible"


def ic_video_info(ff, fp):
    """(秒數, 有沒有音軌, 畫面大小)。"""
    t = subprocess.run([ff, "-hide_banner", "-i", fp], capture_output=True, timeout=30).stderr.decode("utf-8", "replace")
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", t)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    v = re.search(r"Stream #[^\n]*Video:[^\n]*?, (\d{2,5})x(\d{2,5})", t)
    return dur, bool(re.search(r"Stream #[^\n]*Audio:", t)), (v.group(1) + "x" + v.group(2)) if v else ""


def ic_frame_at(ff, fp, t):
    r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-ss", "%.3f" % t, "-i", fp, "-frames:v", "1",
                        "-f", "image2", "-vcodec", "png", "pipe:1"], capture_output=True, timeout=60)
    return r.stdout if r.returncode == 0 else b""


def ic_face_backoff(rid, i, rec, s):
    """往前找最近一個看得到臉的畫面，回傳 (相對路徑, 秒數)；確定都沒有回 None。
    不用重算：少幾秒結尾，比多等二十分鐘划算。上限 3 秒或三成，再多就是砍劇情了。
    「沒辦法看」要丟例外，不能回 None：回 None 會把好好的影片丟掉重算，白花二十分鐘。"""
    ff, vrel = find_ffmpeg(), s["video"].replace("\\", "/")
    if not ff:
        return None                     # 永遠不可能往前找，暫停只會一直卡住
    fp = None
    for a in range(4):
        fp = media_fetch(vrel)
        if fp:
            break
        _media_fetch_fail.pop(media_path(vrel), None)
        ic_nap(rid, 15)
    if not fp:
        raise IOError("拿不到第 %d 章的影片，沒辦法檢查結尾；影片沒有動，按「繼續生成」再試一次" % (i + 1))
    dur = ic_video_info(ff, fp)[0]
    if dur <= 0:
        raise IOError("讀不出第 %d 章影片的長度，沒辦法檢查結尾" % (i + 1))
    limit, back, unsure = min(3.0, rec["seg"] * 0.3), 0.25, False
    while back <= limit + 1e-6 and dur - back > 0:
        ic_check_stop(rid)
        ic_stat(rid, "第 %d 章結尾看不到臉，往前找畫面（-%.2f 秒）…" % (i + 1, back))
        raw = ic_frame_at(ff, fp, dur - back)
        ok = ic_face_ok(rid, raw, rec["plan"].get("identity")) if raw else None
        if ok:
            rel = re.sub(r"\.(mp4|webm|mkv|mov)$", "-cut-frame.png", vrel, flags=re.I)
            with open(media_path(rel), "wb") as f:
                f.write(raw)
            return rel, round(dur - back, 3)
        unsure = unsure or ok is None
        back += 0.25
    if unsure:
        raise IOError("第 %d 章結尾有幾個畫面判斷不出來（LLM 可能忙或斷線）；影片沒有動，按「繼續生成」再試一次" % (i + 1))
    return None


def ic_mark_next_stale(rid, i):
    # 下一章如果已經照舊畫面寫過 prompt，就不能用了（手改過的除外）
    with LOCK:
        rec = ic_load(rid)
        nx = rec["chapters"][i + 1]
        if nx["content"] and not nx["edited"]:
            nx["stale"] = True
            ic_commit(rec)


def ic_face_guard(rid, i, spec):
    """第 i 章的結尾能不能接下一章。下一章只看得到這一幀，沒有臉的話臉就會變成隨機的。
    放在接下一章之前檢查（不是算完就檢查）：重啟、改用舊片都會經過這裡。
    回傳 True＝可以接；False＝這一章退回重算了。"""
    if not spec.get("face_guard", True):
        return True
    rec = ic_load(rid)
    s = rec["chapters"][i]
    if s.get("face_checked") or s.get("face_skip"):
        return True
    if s.get("face_fail"):
        raise IOError(IC_FACE_FAIL % (i + 1))
    # 下一章已經送出去了：它的第一幀早就定了，這時才改接點，收回來的影片會對不上
    if rec["chapters"][i + 1].get("pid"):
        return True
    ic_stat(rid, "檢查第 %d 章的結尾畫面…" % (i + 1))
    ic_gpu_wait(rid, "llama", i + 1)
    ok = ic_face_ok(rid, ic_first_frame(rid, rec, i + 1), rec["plan"].get("identity"))
    if ok is None:
        return True                     # 這次判斷不出來：放行但不記成已檢查，下次還會再看
    if ok:
        ic_ch(rid, i, face_checked=True)
        return True
    hit = ic_face_backoff(rid, i, rec, s)
    ic_check_stop(rid)
    if hit:
        with LOCK:
            rec = ic_load(rid)
            c = rec["chapters"][i]
            c.update(last_frame=hit[0], cut=hit[1], face_checked=True)
            # 下一章的舊片是接這支原本的結尾，現在接點往前移了，不能再選回來
            for t in rec["chapters"][i + 1]["takes"]:
                if t.get("parent") == c["video"]:
                    t["parent"] = ""
            ic_commit(rec)
        ic_mark_next_stale(rid, i)
        ic_update(rid, msg="第 %d 章結尾看不到臉，下一章改接 %.2f 秒的畫面" % (i + 1, hit[1]))
        return True
    tries = int(s.get("face_tries") or 0)
    if tries >= IC_FACE_RETRIES:
        ic_ch(rid, i, face_fail=True)
        raise IOError(IC_FACE_FAIL % (i + 1))
    with LOCK:
        rec = ic_load(rid)
        c = rec["chapters"][i]
        c["takes"] = (c["takes"] + [{"video": c["video"], "ts": time.strftime("%Y/%m/%d %H:%M:%S"),
                                     "content": c.get("sent") or c["content"], "snap": c.get("snap"),
                                     "parent": c.get("parent") or "", "last_frame": c.get("last_frame") or "",
                                     "cut": c.get("cut") or 0, "face_rejected": True}])[-30:]
        c.update(video="", pid="", last_frame="", cut=0, face_tries=tries + 1)
        rec["msg"] = "第 %d 章結尾看不到臉，換 seed 重算（第 %d 次）" % (i + 1, tries + 1)
        ic_commit(rec)
    ic_mark_next_stale(rid, i)
    return False


def ic_do_chapter(rid, i, spec):
    with IC_LOCK:
        IC_CUR["ch"] = i
    if i > 0 and not ic_face_guard(rid, i - 1, spec):
        return                                     # 上一章退回重算，主迴圈會回頭做它
    rec = ic_ch(rid, i, error="", vstate="")
    raw = ic_first_frame(rid, rec, i)
    s = rec["chapters"][i]
    if not s["pid"] and (not s["content"] or s["stale"]):
        ic_write(rid, i, raw, spec)
        ic_check_stop(rid)
        s = ic_load(rid)["chapters"][i]
    if not s["pid"] and s.get("hard") and not s["edited"]:
        ic_ch(rid, i, stale=True, vstate="")       # 下次按繼續就重寫，不會卡在同一份壞掉的內容
        raise IOError("prompt 沒通過檢查：" + "；".join(s.get("v_e") or ["請重寫或手動修改"])[:120])
    ic_render(rid, i, raw, spec)


def ic_clear_vstate(rid):
    with LOCK:
        rec = ic_load(rid)
        for c in rec["chapters"]:
            c["vstate"] = ""
        ic_commit(rec)


def ic_write_only(rid, only, spec):
    """只寫這一章的 prompt，不送影片。"""
    try:
        ic_update(rid, msg="第 %d 章寫 prompt 中…" % (only + 1))
        ic_write(rid, only, ic_first_frame(rid, ic_load(rid), only), spec)
        fields = {"msg": "第 %d 章的 prompt 寫好了" % (only + 1)}
        if spec.get("prev_status"):
            fields["status"] = spec["prev_status"]
        ic_update(rid, **fields)
    except Exception as e:
        msg = getattr(e, "msg", None) or str(e)
        ic_ch(rid, only, vstate="", error=msg[:400])
        fields = {"msg": "✘ " + msg[:280]}
        if spec.get("prev_status"):
            fields["status"] = spec["prev_status"]
        ic_update(rid, **fields)


def ic_write_only_job(rid, only, spec):
    try:
        ic_write_only(rid, only, spec)
    except Exception as e:
        slog("  continue write: " + str(e)[:200])
    finally:
        with IC_LOCK:
            IC_PLANNING.discard(rid)
            IC_STAT.pop(rid, None)


def ic_run_project(rid):
    try:
        with open(os.path.join(CONTINUES, rid + ".run.json"), encoding="utf-8") as f:
            spec = json.load(f)
    except Exception:
        ic_update(rid, status="paused", msg="✘ 讀不到這次的生成設定，請重新按開始")
        return
    only = spec.get("write_only")
    if only is not None:          # 更新前排進佇列、重啟後才輪到的「只寫 prompt」
        ic_write_only(rid, only, spec)
        return
    ic_update(rid, status="running", msg="")
    try:
        while True:
            rec = ic_load(rid)
            i = next((k for k, c in enumerate(rec["chapters"]) if not c["video"]), None)
            if i is None:
                ic_update(rid, status="done", msg="全部章節完成——可以逐章查看，或按「合併成長片」")
                return
            ic_check_stop(rid)
            if IC_STOP_AFTER[0]:
                raise IcPaused()
            tries = rec["chapters"][i].get("face_tries") or 0
            ic_update(rid, msg="第 %d/%d 章%s" % (i + 1, len(rec["chapters"]), ("（結尾看不到臉，第 %d 次重算）" % tries) if tries else ""))
            ic_do_chapter(rid, i, spec)
    except IcStopped:
        with LOCK:
            rec = ic_load(rid)
            i = next((k for k, c in enumerate(rec["chapters"]) if not c["video"]), 0)
            rec["chapters"][i].update(pid="", vstate="", error="")
            rec.update(status="paused", msg="第 %d 章已停止——可以修改 prompt，再按「繼續生成」" % (i + 1))
            ic_commit(rec)
    except IcPaused:
        ic_clear_vstate(rid)
        ic_update(rid, status="paused", msg="已停止，按「繼續生成」接著做；送出的工作算完會收回來")
    except Exception as e:
        msg = getattr(e, "msg", None) or str(e)
        with LOCK:
            rec = ic_load(rid)
            i = next((k for k, c in enumerate(rec["chapters"]) if not c["video"]), None)
            if i is not None:
                rec["chapters"][i].update(error=msg[:400], vstate="")
            rec.update(status="paused", msg="✘ " + msg[:280])
            ic_commit(rec)


def icworker():
    """一次只跑一個專案、一章接一章。GPU 也只有一張。"""
    while True:
        IC_WAKE.wait(5.0)
        IC_WAKE.clear()
        while True:
            with IC_LOCK:
                if not IC_Q:
                    break
                rid = IC_Q.pop(0)
                IC_CUR.update(id=rid, ch=-1)
                IC_CANCEL.pop(rid, None)
            try:
                ic_run_project(rid)
            except Exception as e:
                slog("  continue worker: " + str(e)[:200])
            waiting = []
            with IC_LOCK:
                IC_CUR.update(id="", ch=-1)
                IC_STAT.pop(rid, None)
                IC_CANCEL.pop(rid, None)
                if IC_STOP_AFTER[0]:
                    IC_STOP_AFTER[0] = False
                    waiting = list(IC_Q)
                    del IC_Q[:]
            for r in waiting:
                # 這條執行緒只有一條，死了之後每一筆都會永遠排隊，而且沒有任何地方會說
                try:
                    ic_update(r, status="paused", msg="已停止，按「繼續生成」接著做")
                except Exception as e:
                    slog("  continue worker: " + str(e)[:200])


def ic_parse_plan(text, n):
    raw = (text or "").strip()
    raw = re.sub(r"```\s*$", "", re.sub(r"^```\s*", "", re.sub(r"^```json\s*", "", raw, flags=re.I)))
    try:
        o = json.loads(raw)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", raw)
        o = json.loads(m.group(0) if m else "")
    chs = o.get("chapters") if isinstance(o.get("chapters"), list) else []
    if n < 1:
        raise ValueError("章數是 0")
    if len(chs) < n:
        raise ValueError("只規劃了 %d/%d 章" % (len(chs), n))
    c = o.get("continuity") or {}
    plan = {"story": str(o.get("story") or ""), "music": str(c.get("music") or "N/A")}
    for k in ("style", "identity", "voice", "setting", "soundscape"):
        plan[k] = str(c.get(k) or "")
    out = []
    for x in chs[:n]:
        b = x.get("beats")
        b = b if isinstance(b, list) else [b or x.get("beat") or ""]
        out.append({"beats": "\n".join(str(y).strip() for y in b if str(y).strip()),
                    "dialogue": str(x.get("dialogue") or "").strip(), "ends_on": str(x.get("ends_on") or "").strip()})
    return plan, out


def ic_plan_job(rid, sys_text, user_text, n):
    prior = "new"
    try:
        prior = ic_load(rid).get("status") or "new"
        ic_update(rid, status="planning", msg="規劃章節中…", n=n)
        rec = ic_load(rid)
        ic_gpu_wait(rid, "llama", 0)
        url = ic_llm_image(ic_first_frame(rid, rec, 0))
        got, err = None, ""
        for a in range(3):
            if a:
                ic_update(rid, msg="規劃結果不完整（%s），重試 %d/2…" % (err, a))
            j = ic_llm([{"role": "system", "content": sys_text},
                        {"role": "user", "content": [{"type": "text", "text": user_text},
                                                     {"type": "image_url", "image_url": {"url": url}}]}],
                       0.8, min(16000, 1500 + n * 500))
            try:
                got = ic_parse_plan(j["choices"][0]["message"].get("content"), n)
                break
            except Exception as e:
                err = "回覆被截斷" if j["choices"][0].get("finish_reason") == "length" else str(e)[:120]
        if not got:
            raise IOError("LLM 沒有給出可用的章節規劃（%s）" % err)
        plan, chs = got
        with LOCK:
            rec = ic_load(rid)
            old = rec["chapters"]
            pids = [c["pid"] for c in old if c.get("pid")]
            new = []
            for k, c in enumerate(chs):
                o = old[k] if k < len(old) else None
                new.append(ic_chapter(dict(c, recipe=o["recipe"] if o else rec.get("recipe") or "",
                                                 hint=o["hint"] if o else "", use_snap=o["use_snap"] if o else False,
                                                 snap=o["snap"] if o else None)))
            # 新規劃到手才動舊章節：LLM 失敗時原本的影片與 prompt 都還在。
            # 舊影片不留在產出紀錄——那是舊劇情的東西，改用會接回舊的故事。
            rec.update(plan=plan, chapters=new, final="", status="planned", n=n,
                       msg="規劃完成——看過各章內容後按「開始生成」")
            ic_commit(rec)
        for pid in pids:
            try:
                comfy_api("/queue", {"delete": [pid]}, timeout=10)
                comfy_api("/interrupt", {"prompt_id": pid}, timeout=10)
            except Exception:
                pass
    except Exception as e:
        try:
            ic_update(rid, status=prior, msg="✘ 規劃失敗：" + str(e)[:240])
        except Exception:
            pass
    finally:
        with IC_LOCK:
            IC_PLANNING.discard(rid)


IC_OWNER = [False]
IC_LOCKFILE = [None]


def ic_take_runner():
    """同一個資料夾只能有一個 h3-server 跑接龍，否則兩邊會各送一次。
    用作業系統的檔案鎖：程式掛掉鎖就放開，重啟照樣接得回去。"""
    os.makedirs(CONTINUES, exist_ok=True)
    f = open(os.path.join(CONTINUES, "runner.lock"), "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    except OSError:
        # Windows 拿不到鎖走這裡；Linux 上則是掛載的檔案系統不支援 flock（Docker 的
        # bind mount 可能如此），那種情況沒辦法判斷，當作只有自己，總比完全不能跑好
        if os.name == "nt":
            return False
    IC_LOCKFILE[0] = f
    return True


def ic_boot():
    """h3-server 重啟後把跑到一半的接回去。"""
    IC_OWNER[0] = ic_take_runner()
    if not IC_OWNER[0]:
        slog("  [continue] 這個資料夾已經有別的 h3-server 在跑接龍，這一個不接手")
        return
    for r in reversed(load_cindex()):
        rid = r.get("id")
        try:
            if r.get("status") in ("queued", "running") and os.path.exists(os.path.join(CONTINUES, str(rid) + ".run.json")):
                ic_clear_vstate(rid)
                with IC_LOCK:
                    IC_Q.append(rid)
            elif r.get("status") == "planning":
                rec = ic_load(rid)
                ic_update(rid, status="planned" if rec["chapters"] else "new", msg="規劃被中斷，請按「重新規劃章節」")
            elif any(c.get("vstate") for c in ic_load(rid)["chapters"]):
                ic_clear_vstate(rid)     # 「只寫 prompt」途中重啟，不清掉那一章會一直顯示寫 prompt 中
        except Exception as e:
            slog("  continue boot: " + str(e)[:200])
    IC_WAKE.set()


# ---------------------------------------------------------------- comfy websocket
# ComfyUI 的進度與預覽事件是「單播給單一 clientId」的：同一個 id 開第二條連線，
# 第一條會靜默收不到東西。所以整台機器只由伺服器開這一條，再分發給所有瀏覽器。
# clientId 必須與送單時用的 client_id 一致，否則收不到自己送的任務的事件。
# 同一台 ComfyUI 接第二個 h3-server（別的 port）時要用不同的 id，否則會把第一個的事件搶走。
WS_CLIENT_ID = "h3-webui"


def _ws_connect():
    import base64, hashlib
    u = urlparse(CONFIG["comfy_url"])
    host = u.hostname or "127.0.0.1"
    port = u.port or (443 if u.scheme == "https" else 80)
    key = base64.b64encode(os.urandom(16)).decode()
    # ComfyUI 放在反向代理後面時，網址可能帶路徑前綴（https://host/comfy），
    # /ws 要接在前綴之後，否則代理根本不會轉給 ComfyUI。
    req = ("GET %s/ws?clientId=%s HTTP/1.1" % (u.path.rstrip("/"), WS_CLIENT_ID),
           "Host: %s:%d" % (host, port), "Upgrade: websocket", "Connection: Upgrade",
           "Sec-WebSocket-Key: " + key, "Sec-WebSocket-Version: 13", "", "")
    sock = socket.create_connection((host, port), timeout=15)
    if u.scheme == "https":
        # 走 https 的 ComfyUI（例如從外網經反向代理連進來）只收 TLS。
        # 以前一律送明文握手，對方直接斷線，進度與即時預覽就永遠收不到，
        # 而送單照常成功——看起來只是「進度條不動」。
        import ssl
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    sock.sendall(("\r\n".join(req)).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        d = sock.recv(4096)
        if not d:
            raise IOError("websocket 握手中斷")
        buf += d
    head, rest = buf.split(b"\r\n\r\n", 1)
    if b"101" not in head.split(b"\r\n")[0]:
        raise IOError("websocket 握手失敗: %s" % head.split(b"\r\n")[0][:80])
    return sock, rest


def _ws_recv(sock, idle):
    """ComfyUI 閒置時一句話都不說，socket 的 15 秒上限一到就丟例外。以前這就
    當成斷線：重連、在主控台留一行，經過反向代理時每 18 秒一次，而且重連的
    那幾秒裡來的進度會漏掉。改成讀不到就送一個 ping（用戶端的幀必須加 mask），
    對方回 pong 就算活著；連三次什麼都沒回才當成真的斷了。"""
    while True:
        try:
            d = sock.recv(65536)
            idle[0] = 0
            return d
        except socket.timeout:
            idle[0] += 1
            if idle[0] > 3:
                raise
            sock.sendall(b"\x89\x80" + os.urandom(4))


def _ws_frames(sock, rest):
    """產出 (opcode, payload)。只處理伺服器->用戶端方向（不會有 mask）。"""
    buf = rest
    idle = [0]
    while True:
        while len(buf) < 2:
            d = _ws_recv(sock, idle)
            if not d:
                return
            buf += d
        b0, b1 = buf[0], buf[1]
        op = b0 & 0x0F
        ln = b1 & 0x7F
        i = 2
        if ln == 126:
            while len(buf) < 4:
                buf += _ws_recv(sock, idle)
            ln = struct.unpack(">H", buf[2:4])[0]; i = 4
        elif ln == 127:
            while len(buf) < 10:
                buf += _ws_recv(sock, idle)
            ln = struct.unpack(">Q", buf[2:10])[0]; i = 10
        while len(buf) < i + ln:
            d = _ws_recv(sock, idle)
            if not d:
                return
            buf += d
        yield op, buf[i:i + ln]
        buf = buf[i + ln:]


def _ws_job_for(pid):
    """prompt_id -> 佇列任務 id。沒帶 prompt_id 的事件套用到目前唯一在跑的那筆。"""
    with QLOCK:
        if pid:
            for r in QROWS.values():
                if r.get("prompt_id") == pid:
                    return r["id"]
            return None
        running = [r for r in QROWS.values() if r.get("state") == "running"]
        return running[0]["id"] if len(running) == 1 else None


def wsworker():
    """唯一一條 ComfyUI websocket：把進度與預覽收進記憶體供 /api/queue 分發。"""
    while True:
        sock = None
        try:
            sock, rest = _ws_connect()
            cur = None
            for op, payload in _ws_frames(sock, rest):
                if op == 8:
                    break
                if op == 1:
                    try:
                        msg = json.loads(payload.decode("utf-8", "replace"))
                    except Exception:
                        continue
                    t = msg.get("type"); d = msg.get("data") or {}
                    # progress 也要算：h3-server 重啟後重連，ComfyUI 補發的 executing 沒有 prompt_id，
                    # 接手算到一半的工作只能靠它認人
                    if t in ("execution_start", "executing", "progress") and d.get("prompt_id"):
                        with QLOCK:
                            if LIVE_ONE["pid"] != d["prompt_id"]:
                                LIVE_ONE.update(pid=d["prompt_id"], mime="", raw=b"", tick=0, step="", progress=None)
                    if t == "executing":
                        cur = _ws_job_for(d.get("prompt_id")) or cur
                        if d.get("node") is None and cur:
                            with QLOCK:
                                QLIVE.pop(cur, None)
                            cur = None
                    elif t == "progress":
                        jid = _ws_job_for(d.get("prompt_id")) or cur
                        mx = d.get("max") or 0
                        if mx and d.get("prompt_id"):
                            with QLOCK:
                                if LIVE_ONE["pid"] == d["prompt_id"]:
                                    LIVE_ONE["progress"] = min(1.0, (d.get("value") or 0) / mx)
                        if jid and mx:
                            with QLOCK:
                                QLIVE[jid] = {"progress": min(1.0, (d.get("value") or 0) / mx),
                                              "stage": str(d.get("node") or "")}
                    elif t == "kj_preview_override":
                        # ModelPreviewOverrideKJ 的即時預覽：base64，mime 可能是 video/mp4 或 image/webp
                        # 用正在算的 prompt_id 找列：只用「清單裡唯一在跑的那列」的話，
                        # 排在 I2VA Continue 章節後面的清單任務會拿到別人的預覽
                        with QLOCK:
                            _now = LIVE_ONE["pid"]
                        jid = cur or _ws_job_for(_now)
                        b64 = d.get("image")
                        if b64:
                            # 訊息裡沒有 prompt_id，靠上面 executing 記下的那一支對應
                            try:
                                _raw = base64.b64decode(b64)
                                with QLOCK:
                                    LIVE_ONE.update(mime=str(d.get("mime") or "image/webp"), raw=_raw, tick=LIVE_ONE["tick"] + 1,
                                                    step=("%s/%s" % (d.get("step"), d.get("total"))) if d.get("total") else LIVE_ONE["step"])
                            except Exception:
                                pass
                        if jid and b64:
                            try:
                                raw = base64.b64decode(b64)
                            except Exception:
                                raw = None
                            if raw:
                                with QLOCK:
                                    QPREV[jid] = (str(d.get("mime") or "image/webp"), raw)
                                    live = QLIVE.get(jid) or {}
                                    if d.get("total"):
                                        live["prev_step"] = "%s/%s" % (d.get("step"), d.get("total"))
                                    live["prev_tick"] = (live.get("prev_tick") or 0) + 1
                                    QLIVE[jid] = live
                    elif t in ("execution_error", "execution_interrupted"):
                        jid = _ws_job_for(d.get("prompt_id")) or cur
                        if jid:
                            with QLOCK:
                                QLIVE.pop(jid, None); QPREV.pop(jid, None)
                elif op == 2:
                    # ComfyUI 內建的二進位預覽：前 8 bytes 是事件型別與影像格式
                    if len(payload) > 8:
                        with QLOCK:
                            if cur:
                                QPREV[cur] = ("image/jpeg", payload[8:])
                            LIVE_ONE.update(mime="image/jpeg", raw=payload[8:], tick=LIVE_ONE["tick"] + 1)
        except Exception as e:
            sys.stderr.write("  comfy ws: " + str(e)[:160] + chr(10))
        finally:
            try:
                if sock: sock.close()
            except Exception:
                pass
        time.sleep(3)     # 斷線後重連

MODE_TO_DIRECTOR = {"t2va": "T2VA", "i2va": "I2VA", "fl2va": "FL2VA", "l2va": "L2VA", "ref2va": "REF2VA"}


def split_fields(content, mode=""):
    """從完整 prompt 拆出三欄位（REF2VA 的主段落取 detailed_description）。標頭有無冒號都吃。"""
    c = str(content or "")
    main = "detailed_description" if str(mode).lower() == "ref2va" else "integrated_multimodal_description"
    def sec(a, b):
        m = re.search(a + r":?\s*", c)
        if not m:
            return ""
        j = re.search(b + r":?\s*", c[m.end():]) if b else None
        return c[m.end(): m.end() + j.start()].strip() if j else c[m.end():].strip()
    out = {"imd": sec(main, "overall_soundscape"),
           "soundscape": sec("overall_soundscape", "non_diegetic_music"),
           "music": sec("non_diegetic_music", None)}
    # 沒有任何欄位標頭（例如 T2VA「直接使用」的中文劇本）→ 整段內容就是主 prompt，
    # 否則影片內嵌 metadata 的 prompt 欄位會是空的
    if not (out["imd"] or out["soundscape"] or out["music"]) and c.strip():
        out["imd"] = c.strip()
    return out


def split_ref_fields(c):
    """REF2VA 六欄位拆解（標頭有無冒號都吃）；至少找到 4 欄才視為有效。"""
    labs = ["subject_definitions", "summary", "retention_analysis",
            "detailed_description", "overall_soundscape", "non_diegetic_music"]
    c = str(c or "")
    found = []
    for l in labs:
        m = re.search(r"(?m)^\s*" + l + r"\s*:?\s*", c)
        if m:
            found.append((l, m.start(), m.end()))
    found.sort(key=lambda x: x[1])
    out = {}
    for i, (l, s, e) in enumerate(found):
        nxt = found[i + 1][1] if i + 1 < len(found) else len(c)
        out[l] = c[e:nxt].strip()
    return out if len(found) >= 4 else None


# ── latent 路徑護欄 ──────────────────────────────────────────────────────
# MinimaxH3LatentUpscaler3D 是 LATENT->LATENT，沒有 CONDITIONING 埠，所以它沒辦法
# 跟著縮放 minimax_keyframes。PackedLayout 依「目標」網格編列 keyframe 的條件列
# （comfy/ldm/minimax/model.py:305,330），而 keyframe 的 latent 是在 Director 畫布
# 尺寸下 VAE 編碼的 —— 兩者對不上，就是 model.py:600 那個
# [798,96] 塞不進 [3192,96] 的 index_put_。minimax_refs 依自己存下的網格編列
# （model.py:350,375），所以 REF2VA 不受影響。
GUIDE_CLASSES     = {"MiniMaxH3DirectorGuide"}
LATENT_RESAMPLERS = {"MinimaxH3LatentUpscaler3D", "LatentUpscale", "LatentUpscaleBy"}
LATENT_PLUMBING   = {"LTXVSeparateAVLatent", "LTXVConcatAVLatent"}
SAMPLER_LATENT_IN = "latent_image"
SWITCH_CLASS      = "DaSiWa_NodeStatusSwitch"
KEYFRAME_MODES    = {"I2VA", "FL2VA", "L2VA", "Image Inpaint"}


def _is_link(v):
    return (isinstance(v, list) and len(v) == 2
            and isinstance(v[0], str) and isinstance(v[1], int))


def strip_latent_resamplers(g, warn):
    """把 DirectorGuide 與取樣器之間的 latent 重取樣節點整條旁路掉。

    等同於在 API 圖上重現 ComfyUI 自己的 mode-4 穿透。路徑上沒有重取樣節點時
    完全不動圖（可逐位元比對）。回傳 (改動, 被移除的節點)。"""
    changes = []
    for sid, node in list(g.items()):
        ref = (node.get("inputs") or {}).get(SAMPLER_LATENT_IN)
        if not _is_link(ref):
            continue
        # 從 latent_image 往回走，走到 guide 為止
        seen, stack, anchor, resamplers, ok = set(), [ref[0]], None, [], True
        while stack:
            nid = stack.pop()
            if nid in seen:
                continue
            seen.add(nid)
            n = g.get(nid)
            if n is None:
                ok = False; break
            ct = n.get("class_type")
            if ct in GUIDE_CLASSES:
                if anchor is None:
                    anchor = nid
                elif anchor != nid:
                    ok = False; break          # 兩個 guide 餵同一個取樣器：不碰
                continue                        # 絕不越過 guide 往上走
            if ct in LATENT_RESAMPLERS:
                resamplers.append(nid)
            elif ct not in LATENT_PLUMBING:
                ok = False; break               # 路徑上有不認得的節點：不碰
            for v in (n.get("inputs") or {}).values():
                if _is_link(v):
                    stack.append(v[0])
        if not resamplers:
            continue                            # 常態：沒東西要拆
        if not ok or anchor is None:
            raise ValueError(
                "模板在 %s 的 latent 路徑上有 %s，但路徑形狀無法辨識，無法自動旁路。"
                "請在 ComfyUI 關掉「Latent Upscaler 2x」後重新匯入工作流。"
                % (sid, ", ".join(sorted({g[r]["class_type"] for r in resamplers}))))
        # 這條鏈原本吃的是 guide 的哪一個輸出？
        slots = {v[1] for nid in seen if nid != anchor
                 for v in (g[nid].get("inputs") or {}).values()
                 if _is_link(v) and v[0] == anchor}
        if len(slots) != 1:
            raise ValueError("無法判斷 %s 要接 %s 的哪個輸出（候選 %s）" % (sid, anchor, sorted(slots)))
        new_ref = [anchor, slots.pop()]
        node["inputs"][SAMPLER_LATENT_IN] = new_ref
        changes.append((sid, ref, new_ref))

    # 清掉被改線後沒人用的節點。DaSiWa_NodeStatusSwitch 的 target_* 是前端專用的
    # 控制線（nodes_status_switch.py 自己寫著 "Target sockets are frontend-only"），
    # 不能算成真正的使用。
    removed = []
    changed = True
    while changed:
        changed = False
        used = set()
        for n in g.values():
            is_sw = n.get("class_type") == SWITCH_CLASS
            for k, v in (n.get("inputs") or {}).items():
                if is_sw and str(k).startswith("target_"):
                    continue
                if _is_link(v):
                    used.add(v[0])
        for nid, n in list(g.items()):
            if n.get("class_type") in (LATENT_RESAMPLERS | LATENT_PLUMBING) and nid not in used:
                del g[nid]; removed.append(nid); changed = True
    # 順手清掉指向已刪節點的前端控制線
    for n in g.values():
        if n.get("class_type") != SWITCH_CLASS:
            continue
        for k, v in list((n.get("inputs") or {}).items()):
            if str(k).startswith("target_") and _is_link(v) and v[0] not in g:
                del n["inputs"][k]
    if changes:
        warn.append("latent upscaler 已旁路：%s；移除 %s"
                    % (", ".join("%s->%s" % (c[0], c[2]) for c in changes), ", ".join(removed)))
    return changes, removed


def comfy_build(imd, soundscape, music, image_name, duration=None, wf=None, image_blob=None,
                mode=None, extra_names=None, full_prompt=None, audio_names=None):
    """載模板，換圖與欄位（＋影片秒數），其餘照舊；種子隨機化避免重複送出被去重。
    extra_data（UI 工作流）同步替換相同欄位——save_metadata 嵌進影片的是它。
    mode/extra_names/full_prompt：四模式支援 —— 設 Director 的 mode、重建 timeline 的圖片清單
    （FL2VA 兩張 slot 0/1、REF2VA 最多 9 張依序、L2VA 單張由 mode 決定當末幀），
    並把網頁生成的完整 prompt 塞進 external_prompt_overwrite（v18 的名稱；Director 以它為最終
    prompt，優先於 builder_state 的重組）。"""
    import random
    with open(COMFY_TEMPLATE, encoding="utf-8") as f:
        tpl = json.load(f)
    if "graph" in tpl and isinstance(tpl.get("graph"), dict):
        g, extra = tpl["graph"], tpl.get("extra_data") or {}
    else:                                  # 舊格式：檔案就是節點圖
        g, extra = tpl, {}
    director = None
    director_id = None
    for nid, node in g.items():
        if node.get("class_type") == "MiniMaxH3Director":
            director, director_id = node, nid
    if director is None:
        raise ValueError("模板裡沒有 MiniMaxH3Director 節點")
    # 只支援 v18：v18 的 Director 有 frame_rate / ref_image_size，舊版沒有；
    # 舊版另有 DaSiWa_ResolutionScaleCalculator。用這兩點辨識並明確擋下。
    _dins = director.get("inputs", {})
    if "frame_rate" not in _dins or "ref_image_size" not in _dins:
        raise ValueError("這個工作流不是 v18（Director 缺少 frame_rate / ref_image_size）。"
                         "本程式只支援 MiniMax H3 v18 工作流，請改用 v18 模板。")
    if any(n.get("class_type") == "DaSiWa_ResolutionScaleCalculator" for n in g.values()):
        raise ValueError("這個工作流是 v18 之前的舊版（含 DaSiWa_ResolutionScaleCalculator）。"
                         "本程式只支援 v18 工作流，請改用 v18 模板。")

    # REF2VA：Director 的 builder_state 用專屬 ref 欄位（subject_definitions/summary/
    # retention_analysis/detailed_description...），不是基礎三欄——塞錯欄位的話，
    # 工作流拖回 ComfyUI 顯示空的 ref 欄，且無 external_prompt 重跑會重組成基礎格式
    ref_secs = None
    if str(mode or "").lower() == "ref2va":
        ref_secs = split_ref_fields(full_prompt)

    def patch_bs(bs):
        # v18：builder_state 走 prompt_mode="simple" 時，Director 只讀 simple_prompt，
        # 底下那些結構化欄位一概不看。送單當下我們用 external_prompt_overwrite 決勝負，
        # 但這裡同步寫入 simple_prompt，影片拖回 ComfyUI 直接重跑才會得到同一份 prompt。
        if full_prompt and str(full_prompt).strip():
            bs["prompt_mode"] = "simple"
            bs["simple_prompt"] = str(full_prompt)
        if ref_secs:
            bs["imd"] = ""
            r = bs.get("ref") if isinstance(bs.get("ref"), dict) else {}
            r["subject_definitions"] = ref_secs.get("subject_definitions", "")
            r["summary"] = ref_secs.get("summary", "")
            r["retention_analysis"] = ref_secs.get("retention_analysis", "")
            r["detailed_description"] = ref_secs.get("detailed_description", "")
            r["soundscape"] = ref_secs.get("overall_soundscape", soundscape)
            r["music"] = ref_secs.get("non_diegetic_music", music) or "N/A"
            # 清空舊短鍵快取，避免節點在長鍵為空時回退到模板殘值
            r["subject_defs"] = []
            r["summary_text"] = ""
            r["retention"] = []
            r["style_line"] = ""
            r["detail"] = ""
            bs["ref"] = r
            bs["soundscape"] = ""
            bs["music"] = ""
            return bs
        bs["imd"] = imd
        bs["soundscape"] = soundscape
        bs["music"] = music
        return bs

    # 每次送單都存 first/last frame PNG（FL2VA Movie 鏈接下一段要用 last-frame）
    for _nid, _node in g.items():
        if _node.get("class_type") == "DaSiWa_EnhancedVideoCombine":
            _node["inputs"]["save_first_frame"] = True
            _node["inputs"]["save_last_frame"] = True

    ins = director["inputs"]
    dmode = MODE_TO_DIRECTOR.get(str(mode or "").lower())
    if dmode:
        ins["mode"] = dmode
    if full_prompt and str(full_prompt).strip():
        # v18：欄位改名為 external_prompt_overwrite，且優先於 build_prompt(builder_state)：
        #   if external_prompt_overwrite.strip(): resolved = external_prompt_overwrite
        # 舊名 external_prompt 在 v18 已不存在，寫進去會被靜默忽略（prompt 等於沒送到）。
        ins["external_prompt_overwrite"] = str(full_prompt)
    old_bs_str = ins.get("builder_state") or "{}"
    old_tl_str = ins.get("timeline_data") or "{}"
    bs = patch_bs(json.loads(old_bs_str))
    if dmode:
        bs["mode"] = dmode
    ins["builder_state"] = json.dumps(bs, ensure_ascii=False)
    tl = json.loads(old_tl_str)
    names = [n for n in ([image_name] + list(extra_names or [])) if n]
    if dmode == "T2VA":
        # 純文字模式：移除圖片項目，其餘（音訊參考等）保留
        tl["items"] = [it for it in tl.get("items", []) if it.get("type") not in ("image", "audio")]
        done = True
    elif dmode:
        # 重建圖片清單：非圖片項目（音訊參考等）保留原樣
        # 音訊項目由這裡全權重建。模板是從 ComfyUI 抓下來的快照，裡面可能留著上次
        # 手動排的參考音訊；留著它們會讓 ref_audio_N 的編號跟 prompt 寫的對不上。
        keep = [it for it in tl.get("items", []) if it.get("type") not in ("image", "audio")]
        img_items = []
        for k, nm in enumerate(names):
            item = {"id": "h3web-img-%d" % (k + 1), "type": "image", "value": nm,
                    "order": k, "enabled": True, "thumbnail": None}
            if dmode == "FL2VA":
                item["slot"] = k          # 0 = 首幀, 1 = 尾幀
            img_items.append(item)
        # 參考音訊接在圖片後面。Director 依 (type_order, slot, index) 排序，
        # audio 的 type_order 是 2，排在圖片之後；這裡的順序決定 ref_audio_1..N 的編號。
        aud_items = []
        for k, nm in enumerate(list(audio_names or [])):
            aud_items.append({"id": "h3web-aud-%d" % (k + 1), "type": "audio", "value": nm,
                              "order": len(img_items) + k, "enabled": True,
                              "trim_start": 0, "trim_end": None, "thumbnail": None})
        tl["items"] = img_items + aud_items + keep
        done = bool(img_items)
    else:
        done = False
        for it in tl.get("items", []):
            if it.get("type") == "image" and it.get("enabled", True) and not done:
                it["value"] = image_name
                it["thumbnail"] = None
                done = True
    if isinstance(tl.get("builder_state"), dict):
        tl["builder_state"] = patch_bs(tl["builder_state"])
    ins["timeline_data"] = json.dumps(tl, ensure_ascii=False)
    if not done:
        raise ValueError("模板的 timeline 裡沒有圖片項目")

    # 比例：依輸入圖的長寬比重算 Director 的 width/height（像素預算沿用模板）。
    # 沒有輸入圖（T2VA）就完全不動，直接用模板值。
    _aspect = (wf or {}).get("aspect")          # 卡片上選的比例；留空＝跟著輸入圖
    if image_blob or _aspect:
        iw, ih = _img_dims(image_blob) if image_blob else (0, 0)
        _preset = (wf or {}).get("resolution_preset") or None
        fit = fit_canvas(iw, ih, ins.get("width"), ins.get("height"), _preset, aspect=_aspect)
        if fit:
            ins["width"], ins["height"] = fit
    apply_wf_params(g, wf)

    # 帶 keyframe 的模式（I2VA/FL2VA/L2VA）一定要把 latent upscaler 拆掉，否則取樣器
    # 必炸；其餘模式預設也拆——它是在全零的 empty latent 上重取樣，實際效果只是把畫布
    # 放大到超出模型 768*1344 的訓練預算，長片段還會 OOM。要留就在 wf 傳 latent_upscaler。
    _eff_mode = dmode or ins.get("mode")
    _keep_upscaler = bool((wf or {}).get("latent_upscaler")) and _eff_mode not in KEYFRAME_MODES
    _stripped = []
    if not _keep_upscaler:
        _warn = []
        _chg, _stripped = strip_latent_resamplers(g, _warn)
        for _w in _warn:
            sys.stderr.write("  comfy_build: %s%s" % (_w, chr(10)))

    # 送出前護欄：這幾個節點被整理掉就等於「回報成功但沒有影片」
    _cls = {n.get("class_type") for n in g.values()}
    for _need in ("MiniMaxH3Director", "DaSiWa_EnhancedVideoCombine", "SamplerCustomAdvanced"):
        if _need not in _cls:
            raise ValueError("整理後的節點圖缺少 %s，拒絕送出" % _need)

    # 影片秒數：網頁寫 prompt 時用的時長要跟 Director 跑的一致（時間戳才不會超出）
    old_dur = ins.get("duration")
    new_dur = None
    try:
        d = int(duration) if duration is not None else None
        if d is not None and 1 <= d <= 30 and isinstance(old_dur, int) and d != old_dur:
            ins["duration"] = d
            new_dur = d
    except (TypeError, ValueError):
        pass

    swaps = {old_bs_str: ins["builder_state"], old_tl_str: ins["timeline_data"]}
    swaps.update(apply_lora_stack(g, wf))
    # 實際執行的 LoRA 串，等下要寫進 UI 圖的 properties（下面的 wf 會被改指到 UI 工作流）
    _lora_stack = (next((n["inputs"].get("stack_data") for n in g.values()
                         if n.get("class_type") == LORA_NODE), None)
                   if isinstance((wf or {}).get("loras"), list) else None)
    # v18 沒有 seed / noise_seed 這兩個輸入了——取樣噪聲來自 DaSiWa_SeedControl，欄位叫
    # seed_value，所以原本只認那兩個名字的隨機化整個沒生效。那個節點只有在 seed_value
    # 為 0 時才會自己重骰（IS_CHANGED 其餘一律回 False），而模板裡存的是 ComfyUI 面板
    # 寫進去的上次 seed，非 0。不換掉它，同一筆紀錄重送就是同一顆 seed、同一支影片，
    # 歷史紀錄那顆「送 ComfyUI 生成」按鈕想做的抽卡等於沒有作用。
    for nid, node in g.items():
        ins_n = node.get("inputs") or {}
        for k, v in list(ins_n.items()):
            if k in ("seed", "noise_seed", "seed_value") and isinstance(v, int) and not isinstance(v, bool):
                nv = random.randint(1, 2**48)   # 不取 0：0 會讓 SeedControl 自己重骰，metadata 就對不上
                ins_n[k] = nv
                swaps[v] = nv
        # 面板狀態跟著新 seed 走，拖回 ComfyUI 時顯示的才是這支影片真正用的那顆
        st = ins_n.get("seed_control_state")
        if isinstance(st, str) and isinstance(ins_n.get("seed_value"), int):
            try:
                sd = json.loads(st)
            except (ValueError, TypeError):
                sd = None
            if isinstance(sd, dict):
                cur = str(ins_n["seed_value"])
                sd["last_seed"] = cur
                prev = sd.get("recent")
                prev = [x for x in prev if x != cur] if isinstance(prev, list) else []
                sd["recent"] = ([cur] + prev)[:20]
                new_st = json.dumps(sd, ensure_ascii=False)
                ins_n["seed_control_state"] = new_st
                swaps[st] = new_st

    # UI 工作流（extra_pnginfo.workflow）逐 widget 同步：值完全相同才替換
    wf = ((extra.get("extra_pnginfo") or {}).get("workflow") or {})
    if _stripped:
        # 把被拆掉的節點在 UI 圖裡標成 bypass（mode 4），拖回 ComfyUI 才是這支影片真正的跑法
        _defs = {str(sg.get("id")): sg for sg in ((wf.get("definitions") or {}).get("subgraphs") or [])}
        _inner = {}                       # 子圖實例 id -> {內部節點 id}
        for _fid in _stripped:
            _parts = str(_fid).split(":")
            _inner.setdefault(":".join(_parts[:-1]) or None, set()).add(_parts[-1])
        def _mark(nodes, ids):
            for _n in (nodes or []):
                if str(_n.get("id")) in ids:
                    _n["mode"] = 4
        _mark(wf.get("nodes"), _inner.get(None, set()))
        for _n in (wf.get("nodes") or []):
            _sg = _defs.get(str(_n.get("type")))
            if _sg:
                _mark(_sg.get("nodes"), _inner.get(str(_n.get("id")), set()))
    for n in wf.get("nodes", []):
        wv = n.get("widgets_values")
        if isinstance(wv, list):
            for i, v in enumerate(wv):
                if isinstance(v, (str, int)) and v in swaps:
                    wv[i] = swaps[v]
            # duration only on the Director's own UI node (a bare int would collide elsewhere)
            if n.get("type") == "MiniMaxH3Director" and str(n.get("id")) == str(director_id):
                if new_dur is not None:
                    for i, v in enumerate(wv):
                        if v == old_dur and isinstance(v, int):
                            wv[i] = new_dur
                            break
                # 內嵌 metadata 也要反映本次實際執行：mode widget（第 0 欄）與 prompt widget（第 1 欄）
                if dmode and len(wv) >= 1 and isinstance(wv[0], str):
                    wv[0] = dmode
                if full_prompt and str(full_prompt).strip() and len(wv) >= 2 and isinstance(wv[1], str):
                    wv[1] = str(full_prompt)

    # LoRA 節點的面板（DaSiWa 的 JS）以 properties.stack_data 為準，同步時還會拿它蓋掉
    # widgets_values。只改 widget 的話，影片拖回 ComfyUI 看到的是模板原本的 LoRA——
    # 在那裡直接重送，用的也會是舊的那組（實測：執行的是新的，拖回去顯示舊的）。
    # 兩份都設成這次實際執行的；不靠「舊值相同才換」，兩份的寫法不保證一字不差。
    if _lora_stack is not None:
        _ui_nodes = list(wf.get("nodes") or [])
        for _sg in ((wf.get("definitions") or {}).get("subgraphs") or []):
            _ui_nodes.extend(_sg.get("nodes") or [])
        for n in _ui_nodes:
            if n.get("type") != LORA_NODE:
                continue
            props = n.setdefault("properties", {})
            if isinstance(props, dict):
                props["stack_data"] = _lora_stack
            wv = n.get("widgets_values")
            if isinstance(wv, list) and wv and isinstance(wv[0], str) and wv[0].lstrip().startswith("["):
                wv[0] = _lora_stack

    extra = dict(extra)
    extra["client_id"] = WS_CLIENT_ID
    return g, extra


def new_id():
    return "%08x%04x" % (int(time.time()), int.from_bytes(os.urandom(2), "big"))


class H(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # 影片串流需要 keep-alive 與正確的中斷重連行為

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def handle(self):
        """Keep-alive sockets get torn down by the browser all the time (video seeking, tab switching,
        closing the lightbox). The stdlib lets that surface as a full traceback from readline(); it is
        noise, not an error - swallow it and let the thread exit quietly."""
        try:
            super().handle()
        except (ConnectionError, OSError):
            pass

    def do_HEAD(self):
        m = re.match(r"^/api/media/file/(.+)$", urlparse(self.path).path)
        if m:
            fp = media_fetch(unquote(m.group(1)))
            if not fp or not os.path.isfile(fp):
                return self.send_json({"error": "not found"}, 404)
            size = os.path.getsize(fp)
            ctype = MEDIA_EXT.get(os.path.splitext(fp)[1].lower(), "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            return
        # 同 do_GET：只有首頁可經靜態處理，其餘一律 404，不對外曝光工作目錄
        if urlparse(self.path).path in ("/", "/index.html"):
            self.path = "/" + PAGE
            return SimpleHTTPRequestHandler.do_HEAD(self)
        return self.send_json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        pth = self.path or ""
        if "/api/" not in pth:
            return
        # The ComfyUI status poll fires every 4 s per job for the whole render (minutes). Echoing each one
        # buries everything useful. The status handler logs state TRANSITIONS itself instead.
        if pth.startswith(("/api/comfy/status/", "/api/comfy/live/", "/api/comfy/preview/", "/api/continues/live")):
            return
        slog("  %s %s" % (self.command, pth))

    def guess_type(self, path):
        """標準函式庫送 text/html 時不帶 charset，瀏覽器會按系統預設去猜而變亂碼。"""
        t = SimpleHTTPRequestHandler.guess_type(self, path)
        base = t.split(";")[0].strip()
        if base.startswith("text/") or base in ("application/javascript", "application/json"):
            return base + "; charset=utf-8"
        return t

    # ---------- helpers ----------
    def send_json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.body_too_big = 0
        if n > MAX_BODY:
            self.body_too_big = n
            # 連線是 keep-alive 的，未讀的 body 會被當成下一個請求行。不去讀它（可能很大），
            # 直接標記關閉連線，呼叫端照常回它的錯誤，回完就斷。
            self.close_connection = True
            return None
        if n <= 0:
            return None
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def rec_path(self, rid):
        if not ID_RE.match(rid or ""):
            return None
        return os.path.join(HIST, rid + ".json")

    def send_media(self, fp, download=False):
        size = os.path.getsize(fp)
        ext = os.path.splitext(fp)[1].lower()
        ctype = MEDIA_EXT.get(ext, "application/octet-stream")
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip()) if rng else None
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:  # 只有尾端長度: bytes=-N
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.end_headers()
                return
            code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        if download:
            from urllib.parse import quote as _q
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + _q(os.path.basename(fp)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            with open(fp, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(1 << 16, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionError, OSError):
            # 瀏覽器播影片時會：抓一小段拿縮圖就斷線、用 Range 跳著抓、關燈箱直接砍連線。
            # 送到一半的 socket 被對方關掉 -> 10054 (ConnectionResetError) / 10053 (ConnectionAbortedError)
            # / BrokenPipe。全部都是正常現象，資料早已送達或對方根本不要了。ConnectionError 是三者的共同基底。
            pass

    # ---------- routes ----------
    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            self.path = "/" + PAGE
            return SimpleHTTPRequestHandler.do_GET(self)

        if p == "/api/lessons":
            return self.send_json(load_lessons())

        if p == "/api/lessons/required":
            return self.send_json(load_required())

        if p == "/api/lessons/pending":
            rows = load_index()
            items = []
            for row in rows:                                # 新的在前
                if not (row.get("has_fb") or row.get("rating")):
                    continue
                if row.get("fb_done"):
                    continue
                fp = os.path.join(HIST, str(row.get("id")) + ".json")
                try:
                    with open(fp, encoding="utf-8") as f:
                        rec = json.load(f)
                except Exception:
                    continue
                if rec.get("fb_done"):
                    continue
                if not (rec.get("rating") or rec.get("fb_tags") or rec.get("fb_note")):
                    continue
                imd = rec.get("content", "")
                i = imd.find("integrated_multimodal_description:")
                j = imd.find("overall_soundscape:")
                if i >= 0:
                    imd = imd[i + len("integrated_multimodal_description:"): j if j > i else None].strip()
                items.append({"id": rec["id"], "mode": rec.get("mode") or "cuts",
                              "rating": rec.get("rating", ""), "fb_tags": rec.get("fb_tags") or [],
                              "fb_note": rec.get("fb_note", ""), "story_note": (rec.get("note") or "")[:200],
                              "imd_excerpt": imd[:700]})
                if len(items) >= 20:
                    break
            return self.send_json({"count": len(items), "items": items})

        if p == "/api/queue":
            with QLOCK:
                rows = sorted([qcompact(r) for r in QROWS.values()],
                              key=lambda r: r.get("ts") or 0, reverse=True)[:300]
                for r in rows:
                    live = QLIVE.get(r["id"]) or {}
                    r["progress"] = live.get("progress")
                    r["stage"] = live.get("stage")
                    prev = QPREV.get(r["id"])
                    r["has_preview"] = bool(prev)
                    r["prev_mime"] = prev[0] if prev else None
                    r["prev_step"] = live.get("prev_step")
                    r["prev_tick"] = live.get("prev_tick")
            return self.send_json({"now": int(time.time()), "rows": rows})

        m = re.match(r"^/api/queue/([0-9a-f]{8,32})$", p)
        if m:
            r = qrow(m.group(1))
            return self.send_json(r) if r else self.send_json({"error": "not found"}, 404)

        m = re.match(r"^/api/queue/([0-9a-f]{8,32})/preview$", p)
        if m:
            with QLOCK:
                item = QPREV.get(m.group(1))
            if not item:
                return self.send_json({"error": "no preview"}, 404)
            mime, buf = item
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(buf)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try: self.wfile.write(buf)
            except Exception: pass
            return

        if p == "/api/settings":
            return self.send_json(load_settings())

        if p == "/api/drafts":
            return self.send_json(load_drafts())

        if p == "/api/comfy/params":
            out = {"presets": RES_PRESETS,
                   "fps": None, "resolution_preset": None, "steps": None, "duration": None,
                   "shift_video": None, "shift_audio": None, "has_template": os.path.exists(COMFY_TEMPLATE)}
            try:
                with open(COMFY_TEMPLATE, encoding="utf-8") as f:
                    tpl = json.load(f)
                g = tpl.get("graph") if isinstance(tpl.get("graph"), dict) else tpl
                for nid, node in g.items():
                    ct = node.get("class_type")
                    ins = node.get("inputs", {})
                    if ct == "BasicScheduler" and out["steps"] is None:
                        out["steps"] = ins.get("steps")
                    if ct == "MiniMaxH3Director" and out["duration"] is None:
                        d = ins.get("duration")
                        if isinstance(d, (int, float)):
                            out["duration"] = int(d)
                    if ct == "MiniMaxH3SigmaShift" and out["shift_video"] is None:
                        out["shift_video"] = ins.get("shift_video")
                        out["shift_audio"] = ins.get("shift_audio")
                    # v18：fps 在 Director 上；畫質在 timeline_data.resolution.resolution
                    if ct == "MiniMaxH3Director":
                        if out["fps"] is None and isinstance(ins.get("frame_rate"), (int, float)):
                            out["fps"] = ins["frame_rate"]
                        if out["resolution_preset"] is None:
                            try:
                                res = (json.loads(ins.get("timeline_data") or "{}") or {}).get("resolution") or {}
                                out["resolution_preset"] = res.get("resolution")
                            except Exception:
                                pass
            except Exception:
                pass
            return self.send_json(out)

        if p == "/api/habits":
            q = parse_qs(urlparse(self.path).query)
            mode = (q.get("mode") or ["cuts"])[0]
            if mode not in ("cuts", "onetake"):
                return self.send_json({"error": "mode 必須是 cuts 或 onetake"}, 400)
            try:
                return self.send_json(habit_stats(mode))
            except Exception as e:
                return self.send_json({"error": "分析失敗: %s" % e}, 500)

        if p == "/api/history":
            return self.send_json(load_index())

        m = re.match(r"^/api/history/([^/]+)$", p)
        if m:
            fp = self.rec_path(unquote(m.group(1)))
            if not fp or not os.path.exists(fp):
                return self.send_json({"error": "not found"}, 404)
            with open(fp, encoding="utf-8") as f:
                return self.send_json(json.load(f))

        m = re.match(r"^/api/orig/([^/]+)\.(jpg|png|webp)$", p)
        if m:
            rid, ext = unquote(m.group(1)), m.group(2)
            fp = os.path.join(HIST, rid + ".orig." + ext) if ID_RE.match(rid) else None
            if not fp or not os.path.exists(fp):
                return self.send_json({"error": "not found"}, 404)
            b = open(fp, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[ext])
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(b)
            return

        m = re.match(r"^/api/audio/([^/]+)/(\d{1,2})\.([a-z0-9]{1,5})$", p)
        if m:
            rid = unquote(m.group(1))
            fp = (os.path.join(HIST, "%s.a%s.%s" % (rid, m.group(2), m.group(3)))
                  if ID_RE.match(rid) else None)
            if not fp or not os.path.exists(fp):
                return self.send_json({"error": "not found"}, 404)
            b = open(fp, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "audio/" + m.group(3))
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Accept-Ranges", "none")
            self.end_headers()
            return self.wfile.write(b)

        m = re.match(r"^/api/extra/([^/]+)/(\d{1,2})\.jpg$", p)
        if m:
            rid = unquote(m.group(1))
            fp = os.path.join(HIST, "%s.x%s.jpg" % (rid, m.group(2))) if ID_RE.match(rid) else None
            if not fp or not os.path.exists(fp):
                return self.send_json({"error": "not found"}, 404)
            b = open(fp, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(b)
            return

        m = re.match(r"^/api/(thumb|full)/([^/]+)\.jpg$", p)
        if m:
            kind, rid = m.group(1), unquote(m.group(2))
            suffix = ".jpg" if kind == "thumb" else ".full.jpg"
            fp = os.path.join(HIST, rid + suffix) if ID_RE.match(rid) else None
            if not fp or not os.path.exists(fp):
                return self.send_json({"error": "not found"}, 404)
            b = open(fp, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(b)
            return

        m = re.match(r"^/api/comfy/status/([0-9a-f-]+)$", p)
        if m:
            try:
                return self.send_json(comfy_state(m.group(1)))
            except Exception as e:
                return self.send_json({"error": "ComfyUI 連不上: %s" % e}, 502)

        if p == "/api/media":
            # fresh=1 是「重新整理」按鈕：跳過 60 秒的清單快取，剛算完的馬上看得到
            data = media_list(fresh="fresh=1" in (urlparse(self.path).query or ""))
            if data is None:
                return self.send_json({"error": "媒體資料夾不存在: " + MEDIA_ROOT}, 404)
            return self.send_json(data)

        m = re.match(r"^/api/media/thumb/(.+)$", p)
        if m:
            rel = unquote(m.group(1))
            tp = media_thumb(rel)
            if tp and os.path.isfile(tp):
                b = open(tp, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "public, max-age=604800")
                self.end_headers()
                return self.wfile.write(b)
            # 以前生不出縮圖就退回原檔。但 <img src> 沒辦法在前端設期限，
            # 而一頁二十張縮圖退成二十張全解析度影片，會把同一個 origin 的
            # 六條連線整個卡死——畫面就是「載很久然後什麼都按不動」。
            # 寧可給一張透明的佔位圖，版面撐得住、不吃頻寬。
            px = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01" b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01" b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(px)))
            self.send_header("Cache-Control", "no-store")   # 下次要再試一次
            self.end_headers()
            return self.wfile.write(px)

        m = re.match(r"^/api/media/file/(.+)$", p)
        if m:
            fp = media_fetch(unquote(m.group(1)))
            if not fp or not os.path.isfile(fp):
                return self.send_json({"error": "not found"}, 404)
            dl = "dl=1" in (urlparse(self.path).query or "")
            return self.send_media(fp, dl)

        if p == "/api/prompts":
            return self.send_json(load_pindex())

        m = re.match(r"^/api/prompts/([^/]+)$", p)
        if m:
            rid = unquote(m.group(1))
            fp = os.path.join(PROMPTS, rid + ".json") if ID_RE.match(rid) else None
            if not fp or not os.path.exists(fp):
                return self.send_json({"error": "not found"}, 404)
            with open(fp, encoding="utf-8") as f:
                return self.send_json(json.load(f))

        if p == "/api/uploads":
            rows = load_uindex()
            # attach live reference counts so the UI can show "used by N records"
            cnt = {}
            for r in load_index():
                u = r.get("upload_id")
                if u:
                    cnt[u] = cnt.get(u, 0) + 1
            for r in rows:
                r["refs"] = cnt.get(r["id"], 0)
            return self.send_json(rows)

        m = re.match(r"^/api/uploads/([0-9a-f]{40})/history$", p)
        if m:
            return self.send_json(upload_history_ids(m.group(1)))

        m = re.match(r"^/api/uploads/([0-9a-f]{40})\.orig\.(jpg|png|webp)$", p)
        if m:
            h, ext = m.group(1), m.group(2)
            fp = os.path.join(UPLOADS, h + ".orig." + ext)
            if not os.path.exists(fp):
                return self.send_json({"error": "not found"}, 404)
            with open(fp, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[ext])
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(data)
            return

        m = re.match(r"^/api/uploads/([0-9a-f]{40})(\.thumb)?\.jpg$", p)
        if m:
            h, is_thumb = m.group(1), bool(m.group(2))
            fp = os.path.join(UPLOADS, h + (".thumb.jpg" if is_thumb else ".jpg"))
            if not os.path.exists(fp) and is_thumb:
                fp = os.path.join(UPLOADS, h + ".jpg")       # no thumb stored - serve the full image
            if not os.path.exists(fp):
                return self.send_json({"error": "not found"}, 404)
            with open(fp, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(data)
            return

        if p == "/api/config":
            # 位址類設定不再送到瀏覽器——一律存在伺服器端 config.json，前端只走同源代理
            return self.send_json({"workflow_current": CONFIG.get("workflow_current", ""),
                                   "gpu_free_mb": CONFIG.get("gpu_free_mb", 4000)})

        if p == "/api/skills":
            # 官方 skills 清單：skills/<id>/SKILL.md 的資料夾
            rows = []
            try:
                for d in sorted(os.listdir(SKILLS_DIR)):
                    sd = os.path.join(SKILLS_DIR, d)
                    if not os.path.isdir(sd) or not os.path.exists(os.path.join(sd, "SKILL.md")):
                        continue
                    desc = ""
                    try:
                        with open(os.path.join(sd, "SKILL.md"), encoding="utf-8") as f:
                            head = f.read(2000)
                        mm = re.search(r"^description:\s*(.+)$", head, re.M)
                        if mm:
                            desc = mm.group(1).strip()[:200]
                    except Exception:
                        pass
                    rows.append({"id": d, "description": desc,
                                 "has_cn": os.path.exists(os.path.join(sd, "SKILL.cn.md"))})
            except OSError:
                pass
            return self.send_json(rows)

        m = re.match(r"^/api/skill/([a-z0-9-]+)$", p)
        if m:
            sd = os.path.join(SKILLS_DIR, m.group(1))
            if not os.path.isdir(sd) or not os.path.exists(os.path.join(sd, "SKILL.md")):
                return self.send_json({"error": "not found"}, 404)
            def rd(fp):
                try:
                    with open(fp, encoding="utf-8") as f:
                        return f.read()
                except OSError:
                    return ""
            refs = {}
            rdir = os.path.join(sd, "references")
            if os.path.isdir(rdir):
                for fn in sorted(os.listdir(rdir)):
                    if fn.lower().endswith((".txt", ".md")):
                        refs[fn] = rd(os.path.join(rdir, fn))
            return self.send_json({"id": m.group(1),
                                   "skill": rd(os.path.join(sd, "SKILL.md")),
                                   "cn": rd(os.path.join(sd, "SKILL.cn.md")),
                                   "references": refs})

        if p == "/api/llama/props":
            maybe_reload_config()
            try:
                return self.send_json(llama_api("/props", timeout=10))
            except Exception as e:
                err = str(e)
            # llama-server 放在反向代理後面時，常常只開放 /v1/*（實測：/props、/health、
            # /slots 都是 nginx 回 401，/v1/chat/completions 照常）。生成完全不受影響，
            # 「測試連線」卻會顯示連不上。退回 /v1/models 拼出同樣形狀的回應，
            # 能回答就代表生成那條路是通的。
            try:
                j = llama_api("/v1/models", timeout=10)
                m = ((j.get("data") or [{}])[0]) or {}
                mm = ((j.get("models") or [{}])[0]) or {}
                return self.send_json({
                    "model_ftype": m.get("id") or mm.get("name") or "?",
                    "modalities": {"vision": "multimodal" in (mm.get("capabilities") or [])},
                    "default_generation_settings": {"n_ctx": (m.get("meta") or {}).get("n_ctx")},
                    "via": "/v1/models（/props 無法存取：%s）" % err[:80]})
            except Exception:
                return self.send_json({"error": err}, 502)

        if p == "/api/cutout/status":
            h = cut_ping()
            comfy_here, llama_here = cut_rivals_local()
            return self.send_json({"installed": cut_installed(), "running": bool(h),
                                   "loaded": bool(h and h.get("loaded")),
                                   "bref_loaded": (h or {}).get("bref_loaded"),
                                   "comfy_local": comfy_here, "llama_local": llama_here,
                                   "blocked": cut_gpu_block(), "dir": CUT_EXTERNAL or CUT_DIR})

        if p == "/api/matte/warm":
            # 不讓預熱踩到 GPU 閘門，否則算圖時右鍵選單會出錯
            if cut_gpu_block():
                return self.send_json({"skipped": "gpu busy"})
            try:
                cut_ensure(wait=8)
                nm = (parse_qs(urlparse(self.path).query).get("model") or [""])[0] or                      (CONFIG.get("matte_model") or MATTE_DEFAULT)
                with urllib.request.urlopen(CUT_URL + "/matte/warm?model=" +
                                            quote(nm), timeout=10) as r:
                    return self.send_json(json.loads(r.read().decode("utf-8")))
            except Exception as e:
                return self.send_json({"skipped": str(e)[:120]})

        if p == "/api/about":
            return self.send_json({"name": APP_NAME, "version": APP_VERSION,
                                   "author": APP_AUTHOR, "author_url": "https://github.com/" + APP_AUTHOR,
                                   "repo": APP_REPO, "repo_url": APP_REPO_URL,
                                   "license": APP_LICENSE, "license_url": APP_REPO_URL + "/blob/main/LICENSE",
                                   "releases_url": APP_REPO_URL + "/releases"})

        if p == "/api/about/update":
            try:
                return self.send_json(check_update(fresh="fresh=1" in (urlparse(self.path).query or "")))
            except urllib.error.HTTPError as e:
                if e.code in (403, 429):
                    return self.send_json({"error": "GitHub 暫時限制查詢次數（未登入每小時 60 次），請稍後再試",
                                           "current": APP_VERSION}, 429)
                return self.send_json({"error": "GitHub 回應 HTTP %d" % e.code, "current": APP_VERSION}, 502)
            except Exception as e:
                return self.send_json({"error": "連不上 GitHub：%s" % str(e)[:160], "current": APP_VERSION}, 502)

        if p == "/api/comfy/loras":
            try:
                rows = comfy_loras(fresh="fresh=1" in (urlparse(self.path).query or ""))
            except Exception as e:
                return self.send_json({"error": "讀不到 ComfyUI 的 LoRA 清單：%s" % str(e)[:200]}, 502)
            return self.send_json({"loras": rows, "node": LORA_NODE})

        if p == "/api/matte/models":
            h = cut_ping()
            ms = matte_models()
            # SAM3 不是 BiRefNet 權重，是另一種方式；放同一個下拉選單是
            # 因為使用者要選的就是「用哪一種去背」。環境不齊就不出現。
            if sam3_ready():
                ms = ms + [{"name": "sam3", "kind": "sam3", "mb": 1665, "image_size": 0,
                            "label": "SAM3（文字指定主體）"}]
            return self.send_json({"models": ms,
                                   "default": CONFIG.get("matte_model") or MATTE_DEFAULT,
                                   "sam3_prompt": SAM3_PROMPT_DEF,
                                   "loaded": (h or {}).get("bref_loaded")})

        if p == "/api/gpu":
            return self.send_json(gpu_mem())

        if p == "/api/movies":
            return self.send_json(load_mindex())

        m = re.match(r"^/api/movies/([^/]+)$", p)
        if m and ID_RE.match(m.group(1)):
            fp = os.path.join(MOVIES, m.group(1) + ".json")
            if not os.path.exists(fp):
                return self.send_json({"error": "not found"}, 404)
            with open(fp, encoding="utf-8") as f:
                return self.send_json(json.load(f))

        m = re.match(r"^/api/movies/([^/]+)/img/(\d{1,2})\.jpg$", p)
        if m and ID_RE.match(m.group(1)):
            fp = os.path.join(MOVIES, "%s.img%s.jpg" % (m.group(1), m.group(2)))
            if not os.path.exists(fp):
                return self.send_json({"error": "not found"}, 404)
            b = open(fp, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(b)
            return

        m = re.match(r"^/api/comfy/live/([0-9a-f-]+)$", p)
        if m:
            with QLOCK:
                on = LIVE_ONE["pid"] == m.group(1)
                return self.send_json({"active": on, "has_preview": bool(on and LIVE_ONE["raw"]),
                                       "mime": LIVE_ONE["mime"] if on else "", "tick": LIVE_ONE["tick"] if on else 0,
                                       "step": LIVE_ONE["step"] if on else "", "progress": LIVE_ONE["progress"] if on else None})

        m = re.match(r"^/api/comfy/preview/([0-9a-f-]+)$", p)
        if m:
            with QLOCK:
                item = (LIVE_ONE["mime"], LIVE_ONE["raw"]) if LIVE_ONE["pid"] == m.group(1) and LIVE_ONE["raw"] else None
            if not item:
                return self.send_json({"error": "no preview"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", item[0])
            self.send_header("Content-Length", str(len(item[1])))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try: self.wfile.write(item[1])
            except Exception: pass
            return

        if p == "/api/continues/live":
            with IC_LOCK:
                out = {"queue": list(IC_Q), "cur": dict(IC_CUR), "planning": sorted(IC_PLANNING),
                       "stat": IC_STAT.get(IC_CUR["id"], ""), "stats": dict(IC_STAT), "stop_after": IC_STOP_AFTER[0]}
            out["revs"] = {r["id"]: r.get("rev", 0) for r in load_cindex() if r.get("open")}
            return self.send_json(out)

        if p == "/api/continues":
            return self.send_json(load_cindex())

        m = re.match(r"^/api/continues/([^/]+)$", p)
        if m and ID_RE.match(m.group(1)):
            fp = os.path.join(CONTINUES, m.group(1) + ".json")
            if not os.path.exists(fp):
                return self.send_json({"error": "not found"}, 404)
            with open(fp, encoding="utf-8") as f:
                return self.send_json(json.load(f))

        m = re.match(r"^/api/continues/([^/]+)/(img|thumb)$", p)
        if m and ID_RE.match(m.group(1)):
            import glob as _g
            hits = _g.glob(os.path.join(CONTINUES, "%s.%s.*" % (m.group(1), m.group(2))))
            if not hits:
                return self.send_json({"error": "not found"}, 404)
            with open(hits[0], "rb") as f:
                b = f.read()
            self.send_response(200)
            self.send_header("Content-Type", MEDIA_EXT.get(os.path.splitext(hits[0])[1].lower(), "image/jpeg"))
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(b)
            return

        if p == "/api/workflows":
            wd = CONFIG["workflow_dir"]
            items = []
            try:
                for fn in sorted(os.listdir(wd)):
                    if fn.lower().endswith(".json"):
                        fp = os.path.join(wd, fn)
                        items.append({"name": fn, "mtime": int(os.path.getmtime(fp)),
                                      "size": os.path.getsize(fp)})
            except OSError as e:
                return self.send_json({"error": "工作流資料夾讀取失敗: %s" % e, "dir": wd, "items": []})
            return self.send_json({"dir": wd, "current": CONFIG.get("workflow_current", ""), "items": items})

        if p == "/api/loras":
            return self.send_json(load_lindex())

        m = re.match(r"^/api/loras/([^/]+)$", p)
        if m:
            rid = unquote(m.group(1))
            fp = os.path.join(LORAS, rid + ".json") if ID_RE.match(rid) else None
            if not fp or not os.path.exists(fp):
                return self.send_json({"error": "not found"}, 404)
            with open(fp, encoding="utf-8") as f:
                return self.send_json(json.load(f))

        if p.startswith("/api/"):
            return self.send_json({"error": "unknown endpoint"}, 404)
        # 不再把工作目錄當靜態站台：這行以前會把整個專案資料夾（含 .git/、config.json、
        # h3-server.py 原始碼）公開給任何連得到的人。網頁本身只需要 "/"（見 do_GET 開頭），
        # 其餘資源都是執行期組出的 /api/、blob: 或 data: 網址。
        return self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        p = urlparse(self.path).path

        if p == "/api/llama/chat":
            maybe_reload_config()
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            if not isinstance(body, dict):
                return self.send_json({"error": "empty body"}, 400)
            with QLOCK: LLAMA_INFLIGHT[0] += 1
            try:
                # 前端 300 秒就放棄，而中止傳不到這裡——llama_api 會繼續坐在
                # urlopen 裡等滿自己的上限，而 LLAMA_INFLIGHT 在那段期間一直 > 0，
                # GPU 閘門會跟著以為 llama 還在用卡。比前端早一點放手。
                return self.send_json(llama_api("/v1/chat/completions", body, timeout=280))
            except Exception as e:
                return self.send_json({"error": "llama-server: %s" % e}, 502)
            finally:
                with QLOCK: LLAMA_INFLIGHT[0] = max(0, LLAMA_INFLIGHT[0] - 1)
                LLAMA_LAST[0] = time.time()

        if p == "/api/config":
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            for k in ("llama_url", "comfy_url", "media_root", "workflow_dir"):
                v = str(body.get(k) or "").strip()
                if v:
                    CONFIG[k] = v.rstrip("/") if k.endswith("_url") else v
            if body.get("gpu_free_mb"):
                try:
                    CONFIG["gpu_free_mb"] = max(500, min(24000, int(body["gpu_free_mb"])))
                except Exception:
                    pass
            with LOCK:
                save_config()
            return self.send_json({"ok": True, "llama_url": CONFIG["llama_url"], "comfy_url": CONFIG["comfy_url"],
                                   "media_root": CONFIG["media_root"], "workflow_dir": CONFIG["workflow_dir"],
                                   "gpu_free_mb": CONFIG["gpu_free_mb"]})

        if p == "/api/workflows/upload":
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            name = os.path.basename(str(body.get("name") or "workflow.json"))
            name = re.sub(r"[^\w\-. 一-鿿぀-ヿ]", "_", name)
            if not name.lower().endswith(".json"):
                name += ".json"
            data = body.get("data")
            if not isinstance(data, (dict, list)):
                return self.send_json({"error": "data 必須是 JSON 物件"}, 400)
            wd = CONFIG["workflow_dir"]
            try:
                os.makedirs(wd, exist_ok=True)
                fp = os.path.join(wd, name)
                if os.path.exists(fp):   # 不覆蓋既有檔，加時間戳
                    name = "%s_%s.json" % (name[:-5], time.strftime("%m%d_%H%M%S"))
                    fp = os.path.join(wd, name)
                with open(fp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
            except OSError as e:
                return self.send_json({"error": "寫入失敗: %s" % e}, 500)
            return self.send_json({"ok": True, "name": name})

        if p == "/api/workflows/select":
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            name = os.path.basename(str(body.get("name") or ""))
            if not name.lower().endswith(".json"):
                return self.send_json({"error": "請指定 .json 工作流檔"}, 400)
            fp = os.path.join(CONFIG["workflow_dir"], name)
            if not os.path.exists(fp):
                return self.send_json({"error": "找不到檔案: %s" % name}, 404)
            try:
                with open(fp, encoding="utf-8") as f:
                    wf = json.load(f)
                info = comfy_template_from_json(wf)
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            except Exception as e:
                return self.send_json({"error": "匯入失敗: %s" % e}, 500)
            CONFIG["workflow_current"] = name
            with LOCK:
                save_config()
            return self.send_json(dict(info, current=name))

        if p == "/api/review/scan":
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            try:
                thr = float(body.get("threshold") or 0.30)
                res = review_scan(str(body.get("video") or ""), scene_thr=max(0.1, min(0.9, thr)))
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            except Exception as e:
                return self.send_json({"error": "掃描失敗: %s" % e}, 500)
            return self.send_json(res)

        if p == "/api/movies":
            # 建立/更新 FL2VA Movie 專案。images 只在建立時帶（dataURL 陣列，存成 {id}.imgN.jpg）
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            os.makedirs(MOVIES, exist_ok=True)
            rid = str(body.get("id") or "")
            if rid and not ID_RE.match(rid):
                return self.send_json({"error": "bad id"}, 400)
            if not rid:
                rid = new_id()
            n_img = 0
            imgs = body.pop("images", None)
            if isinstance(imgs, list) and imgs:
                for k, durl in enumerate(imgs[:MV_MAX_IMAGES]):
                    b, _ = parse_data_image(durl)
                    if b:
                        n_img += 1
                        with open(os.path.join(MOVIES, "%s.img%d.jpg" % (rid, k + 1)), "wb") as f:
                            f.write(b)
            segs = body.get("segments") if isinstance(body.get("segments"), list) else []
            rec = {"id": rid, "ts": body.get("ts") or time.strftime("%Y-%m-%d %H:%M:%S"),
                   "up_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "title": str(body.get("title") or "")[:80],
                   "aspect": str(body.get("aspect") or "auto")[:12],
                   "recipe": str(body.get("recipe") or "")[:40],
                   "global_hint": str(body.get("global_hint") or "")[:2000],
                   "global_plan": str(body.get("global_plan") or "")[:4000],
                   "status": str(body.get("status") or "editing")[:20],
                   "final": str(body.get("final") or "")[:300],
                   "segments": [{
                       "hint": str(s.get("hint") or "")[:2000],
                       "beat": str(s.get("beat") or "")[:1000],
                       "content": str(s.get("content") or ""),
                       "zh": str(s.get("zh") or ""),
                       "state": str(s.get("state") or "")[:10],
                       # 生成這段劇情時用的秒數：時間軸是照它寫的，送出時要比對
                       "dur_gen": s.get("dur_gen") if isinstance(s.get("dur_gen"), int) else None,
                       "locked": bool(s.get("locked")),
                       "video": str(s.get("video") or "")[:300],
                       "ai_score": s.get("ai_score"),
                       "ai_problems": [str(x)[:300] for x in (s.get("ai_problems") or [])][:8],
                       # 每次產出的影片（自動重試/再賭/重跑都留下來，可回頭選用）
                       "takes": [{"video": str(t.get("video") or "")[:300], "score": t.get("score")}
                                 for t in (s.get("takes") or []) if isinstance(t, dict)][:20],
                   } for s in segs[:MV_MAX_IMAGES]]}
            with LOCK:
                old = {}
                fp = os.path.join(MOVIES, rid + ".json")
                try:
                    with open(fp, encoding="utf-8") as f:
                        old = json.load(f)
                except Exception:
                    pass
                if not n_img:
                    n_img = int(old.get("n_images") or 0)
                rec["n_images"] = n_img
                rec["ts"] = old.get("ts") or rec["ts"]
                with open(fp, "w", encoding="utf-8") as f:
                    json.dump(rec, f, ensure_ascii=False, indent=1)
                rows = [r for r in load_mindex() if r.get("id") != rid]
                rows.insert(0, {"id": rid, "ts": rec["ts"], "up_ts": rec["up_ts"], "title": rec["title"],
                                "n_images": n_img, "n_segs": len(rec["segments"]),
                                "status": rec["status"], "final": rec["final"]})
                save_mindex(rows)
            return self.send_json({"ok": True, "id": rid})

        m = re.match(r"^/api/comfy/cancel/([0-9a-f-]+)$", p)
        if m:
            # 排隊中的從佇列拿掉，正在算的中斷。一定要帶 prompt_id，不然會停到別人的工作。
            try:
                comfy_api("/queue", {"delete": [m.group(1)]}, timeout=15)
                comfy_api("/interrupt", {"prompt_id": m.group(1)}, timeout=15)
            except Exception as e:
                return self.send_json({"error": "ComfyUI 連不上: %s" % e}, 502)
            return self.send_json({"ok": True})

        if p == "/api/continues/stop-after":
            # 一定要把 body 讀掉：連線是 keep-alive，沒讀的兩個位元組會變成下一個請求的開頭
            try:
                self.read_json()
            except Exception:
                pass
            with IC_LOCK:
                if IC_CUR["id"]:
                    IC_STOP_AFTER[0] = True
            return self.send_json({"ok": True})

        m = re.match(r"^/api/continues/([0-9a-f]{8,32})/(run|plan|stop)$", p)
        if m:
            rid, act = m.group(1), m.group(2)
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            body = body if isinstance(body, dict) else {}
            if not os.path.exists(os.path.join(CONTINUES, rid + ".json")):
                return self.send_json({"error": "not found"}, 404)
            if act == "stop":
                with IC_LOCK:
                    queued = rid in IC_Q
                    if queued:
                        IC_Q.remove(rid)
                    elif IC_CUR["id"] != rid:
                        return self.send_json({"ok": True, "idle": True})
                    else:
                        IC_CANCEL[rid] = "stopping"
                if queued:
                    ic_update(rid, status="paused", msg="已取消排隊")
                    return self.send_json({"ok": True})
                pid = next((c["pid"] for c in ic_load(rid)["chapters"] if not c["video"] and c["pid"]), "")
                try:
                    if pid:   # 一定要帶 prompt_id，不然會停到別人的工作
                        comfy_api("/queue", {"delete": [pid]}, timeout=15)
                        comfy_api("/interrupt", {"prompt_id": pid}, timeout=15)
                except Exception as e:
                    with IC_LOCK:
                        IC_CANCEL.pop(rid, None)
                    return self.send_json({"error": "ComfyUI 連不上，工作還在跑: %s" % e}, 502)
                with IC_LOCK:
                    if IC_CUR["id"] == rid:
                        IC_CANCEL[rid] = "stopped"
                return self.send_json({"ok": True})
            if not IC_OWNER[0]:
                return self.send_json({"error": "這個資料夾已經有別的 h3-server 在跑接龍"}, 409)
            with IC_LOCK:
                if rid in IC_Q or rid in IC_PLANNING or IC_CUR["id"] == rid:
                    return self.send_json({"error": "這筆已經在排隊或生成中"}, 409)
                if act == "plan":
                    IC_PLANNING.add(rid)
                else:
                    if not isinstance(body.get("chapters"), list):
                        return self.send_json({"error": "缺少生成設定"}, 400)
                    if len(body["chapters"]) != len(ic_load(rid)["chapters"]):
                        return self.send_json({"error": "章節數對不上，請重新整理網頁"}, 409)
                    # 只寫 prompt 不用算圖，排在影片佇列後面要等一整條接龍。跟規劃一樣馬上做。
                    if body.get("write_only") is not None:
                        only = _i(body.get("write_only"), 0, IC_MAX_CHAPTERS - 1)
                        IC_PLANNING.add(rid)
                        threading.Thread(target=ic_write_only_job, args=(rid, only, body), daemon=True).start()
                        return self.send_json({"ok": True})
                    body["prev_status"] = ic_load(rid).get("status")
                    _atomic_json(os.path.join(CONTINUES, rid + ".run.json"), body)
                    ic_update(rid, status="queued", msg="排隊中")
                    IC_Q.append(rid)
            if act == "plan":
                threading.Thread(target=ic_plan_job, daemon=True,
                                 args=(rid, str(body.get("sys") or ""), str(body.get("user") or ""),
                                       _i(body.get("n"), 1, IC_MAX_CHAPTERS, 1))).start()
            IC_WAKE.set()
            return self.send_json({"ok": True})

        if p == "/api/continues":
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            if not isinstance(body, dict):
                return self.send_json({"error": "內容太大或格式不對"}, 413)
            try:
                rid, rev = ic_save(body)
                return self.send_json({"ok": True, "id": rid, "rev": rev})
            except IcConflict as e:
                return self.send_json({"conflict": True, "rec": e.rec}, 409)
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)

        if p == "/api/movie/concat":
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            files = [str(x) for x in (body.get("files") or []) if str(x).strip()]
            try:
                out = movie_concat(files, body.get("cuts"))
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            except Exception as e:
                return self.send_json({"error": "串接失敗: %s" % e}, 500)
            return self.send_json({"ok": True, "out": out, "segments": len(files)})

        if p == "/api/comfy/free":
            try:
                comfy_free()
            except Exception as e:
                return self.send_json({"error": "ComfyUI free 失敗: %s" % e, "gpu": gpu_mem()}, 502)
            time.sleep(1.5)
            return self.send_json({"ok": True, "gpu": gpu_mem()})

        if p == "/api/cutout/free":
            # 跟「強制 Free ComfyUI」成對。去背服務閒置三分鐘才自己卸，
            # 想馬上把卡讓出來給別的東西用時等不了那麼久。
            if not cut_ping(timeout=2):
                return self.send_json({"ok": True, "state": "not_running", "gpu": gpu_mem()})
            r = cut_unload()
            if r is None:
                return self.send_json({"error": "去背服務沒有回應", "gpu": gpu_mem()}, 502)
            time.sleep(0.8)
            return self.send_json({"ok": True, "state": "unloaded", "detail": r, "gpu": gpu_mem()})

        if p == "/api/gpu/prepare":
            # 身分感知協調：VRAM 數字分不出持有者，改用可靠訊號 ——
            #   llama 使用中/剛用完：由 /api/llama/chat 代理精確追蹤（in-flight 計數＋最後完成時間）
            #   ComfyUI 忙碌：問它的 /queue
            # 規則：同對象連續使用直接放行（ComfyUI 自己會排佇列、llama 權重還駐留），
            #       只有 llama <-> ComfyUI「交接」才需要等待/釋放。
            try:
                body = self.read_json()
            except Exception:
                body = {}
            d = gpu_prepare_step(str(body.get("target") or ""))
            return self.send_json(d, 400 if d.get("error") else 200)

        if p == "/api/history/clear":
            # SAFETY: "clear all" is the single most destructive action in the app. It never deletes -
            # it moves everything into history_trash/<timestamp>/ so a mistake is recoverable, and it
            # requires an explicit confirm token so a stray request cannot wipe the library.
            try:
                body = self.read_json()
            except Exception:
                body = {}
            if not isinstance(body, dict) or body.get("confirm") != "CLEAR ALL HISTORY":
                return self.send_json({"error": "refused: send {\"confirm\": \"CLEAR ALL HISTORY\"}"}, 400)
            import shutil
            stamp = time.strftime("%Y%m%d-%H%M%S")
            trash = os.path.join(ROOT, "history_trash", stamp)
            moved = 0
            with LOCK:
                os.makedirs(trash, exist_ok=True)
                for f in os.listdir(HIST):
                    src = os.path.join(HIST, f)
                    if not os.path.isfile(src):
                        continue
                    try:
                        shutil.move(src, os.path.join(trash, f)); moved += 1
                    except OSError:
                        pass
                save_index([])
            return self.send_json({"ok": True, "moved": moved, "trash": trash,
                                   "restore_hint": "move the files in %s back into history/ and restart" % trash})

        if p == "/api/lessons":
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            en = str(body.get("en") or "").strip()[:300]
            zh = str(body.get("zh") or "").strip()[:300]
            mode = body.get("mode") if body.get("mode") in MODES + ("all",) else "all"
            if not en and not zh:
                return self.send_json({"error": "en / zh 至少要有一個"}, 400)
            with LOCK:
                rows = load_lessons()
                rid = str(body.get("id") or "")
                hit = next((x for x in rows if x.get("id") == rid), None) if rid else None
                if hit is None:
                    # 完全相同的規則不重複入庫（措辭近似的交給前端相似度比對＋使用者裁決）
                    ne, nz = _les_norm(en or zh), _les_norm(zh or en)
                    dup = next((x for x in rows if _les_norm(x.get("en")) == ne or _les_norm(x.get("zh")) == nz), None)
                    if dup is not None:
                        return self.send_json(dict(dup, dup=True))
                    hit = {"id": new_id(), "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "origin": str(body.get("origin") or "manual")[:10]}
                    rows.insert(0, hit)
                hit.update({"en": en or zh, "zh": zh or en, "mode": mode,
                            "ban": str(body.get("ban") or "")[:200],
                            "enabled": bool(body.get("enabled", True)),
                            "src": [str(x)[:16] for x in (body.get("src") or [])][:20] or hit.get("src", [])})
                save_lessons(rows)
            return self.send_json(hit)

        if p == "/api/lessons/required":
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            def clampint(v, lo, hi):
                try:
                    n = int(v)
                except Exception:
                    return 0
                return max(lo, min(hi, n))
            lang = str(body.get("lang_lock") or "any").lower()
            if lang not in ("any", "japanese", "english", "chinese", "korean"):
                lang = "any"
            v = {"min_words": clampint(body.get("min_words"), 0, 9999),
                 "lang_lock": lang,
                 "line_min": clampint(body.get("line_min"), 0, 999),
                 "line_max": clampint(body.get("line_max"), 0, 999)}
            with LOCK:
                save_required(v)
            return self.send_json(v)

        if p == "/api/lessons/mark":
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            ids = [str(x) for x in (body.get("ids") or []) if ID_RE.match(str(x))]
            done = 0
            with LOCK:
                rows = load_index()
                for rid in ids:
                    fp = os.path.join(HIST, rid + ".json")
                    try:
                        with open(fp, encoding="utf-8") as f:
                            rec = json.load(f)
                        rec["fb_done"] = True
                        with open(fp, "w", encoding="utf-8") as f:
                            json.dump(rec, f, ensure_ascii=False, indent=1)
                        for row in rows:
                            if row.get("id") == rid:
                                row["fb_done"] = 1
                        done += 1
                    except Exception:
                        pass
                save_index(rows)
            return self.send_json({"ok": True, "marked": done})

        m = re.match(r"^/api/history/([^/]+)/review$", p)
        if m:
            rid = unquote(m.group(1))
            if not ID_RE.match(rid):
                return self.send_json({"error": "bad id"}, 400)
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            try:
                score = max(0, min(10, float(body.get("score"))))
            except Exception:
                return self.send_json({"error": "score 必須是 0-10 的數字"}, 400)
            rv = {"score": round(score, 1),
                  "action": str(body.get("action") or "")[:10],
                  "hard_cut": bool(body.get("hard_cut")),
                  "cut_time": body.get("cut_time"),
                  "story_match": body.get("story_match"),
                  "ending_ok": bool(body.get("ending_ok", True)),
                  "problems": [str(x)[:300] for x in (body.get("problems") or [])][:8],
                  "tries": int(body.get("tries") or 0),
                  "video": str(body.get("video") or "")[:300],
                  "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
            fp = os.path.join(HIST, rid + ".json")
            if not os.path.exists(fp):
                return self.send_json({"error": "not found"}, 404)
            with LOCK:
                with open(fp, encoding="utf-8") as f:
                    rec = json.load(f)
                rec["review"] = rv
                with open(fp, "w", encoding="utf-8") as f:
                    json.dump(rec, f, ensure_ascii=False, indent=1)
                rows = load_index()
                for row in rows:
                    if row.get("id") == rid:
                        row["ai_score"] = rv["score"]
                        break
                save_index(rows)
            return self.send_json({"ok": True})

        m = re.match(r"^/api/history/([^/]+)/rate$", p)
        if m:
            rid = unquote(m.group(1))
            if not ID_RE.match(rid):
                return self.send_json({"error": "bad id"}, 400)
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            rating = str(body.get("rating") or "")
            if rating not in ("up", "down", ""):
                return self.send_json({"error": "rating 必須是 up / down / 空字串"}, 400)
            tags = [str(t)[:30] for t in (body.get("fb_tags") or []) if str(t).strip()][:10]
            note = str(body.get("fb_note") or "")[:500]
            fp = os.path.join(HIST, rid + ".json")
            if not os.path.exists(fp):
                return self.send_json({"error": "not found"}, 404)
            with LOCK:
                with open(fp, encoding="utf-8") as f:
                    rec = json.load(f)
                rec["rating"], rec["fb_tags"], rec["fb_note"] = rating, tags, note
                rec["fb_ts"] = time.strftime("%Y-%m-%d %H:%M:%S") if (rating or tags or note) else ""
                rec["fb_done"] = False                     # 重新評分 -> 重新列入待整理
                with open(fp, "w", encoding="utf-8") as f:
                    json.dump(rec, f, ensure_ascii=False, indent=1)
                rows = load_index()
                for row in rows:
                    if row.get("id") == rid:
                        row["rating"] = rating
                        row["has_fb"] = bool(rating or tags or note)
                        row["fb_done"] = 0
                        break
                save_index(rows)
            return self.send_json({"ok": True, "rating": rating, "fb_tags": tags})

        if p == "/api/comfy/template":
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            try:
                info = comfy_template_from_json(body)
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            except Exception as e:
                return self.send_json({"error": "匯入失敗: %s" % e}, 500)
            return self.send_json(info)

        if p == "/api/comfy/refresh":
            try:
                info = comfy_capture_template()
            except Exception as e:
                return self.send_json({"error": "ComfyUI 連不上: %s" % e}, 502)
            if not info:
                return self.send_json({"error": "ComfyUI 歷史裡沒有成功的 MiniMaxH3 生成"}, 404)
            return self.send_json(info)

        if p == "/api/settings":
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            if not isinstance(body, dict) or not isinstance(body.get("values"), dict):
                return self.send_json({"error": "body 需要 {values:{...}}"}, 400)
            cur = load_settings()
            vals = dict(cur.get("values") or {})
            for k, v in body["values"].items():
                if k in SHARED_DEFAULTS:
                    vals[k] = v
            return self.send_json(save_settings(vals, int(cur.get("rev") or 0) + 1))

        if p == "/api/drafts":
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            if not isinstance(body, dict):
                return self.send_json({"error": "body must be an object"}, 400)
            return self.send_json(save_drafts(body))

        # 草稿用的圖片：登記進 uploads（內容雜湊去重），草稿只存 hash
        if p == "/api/uploads/register":
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            full, _ = parse_data_image((body or {}).get("full"))
            if full is None:
                return self.send_json({"error": "需要 full 的 dataURL"}, 400)
            thumb, _ = parse_data_image((body or {}).get("thumb"))
            orig, oext = parse_data_image((body or {}).get("orig"))
            try:
                h = upload_register(full, thumb or b"", str((body or {}).get("name") or "draft.jpg"),
                                    orig or b"", oext if orig else "")
            except Exception as e:
                return self.send_json({"error": str(e)[:200]}, 500)
            return self.send_json({"hash": h})

        if p == "/api/queue":
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            if not isinstance(body, dict):
                return self.send_json({"error": "body must be an object"}, 400)
            row = qenqueue(body.get("payload") if isinstance(body.get("payload"), dict) else body,
                           mode=str(body.get("mode") or ""),
                           title=str(body.get("title") or ""),
                           rec_id=str(body.get("rec_id") or ""),
                           idem=str(body.get("idem") or ""))
            return self.send_json({"id": row["id"], "state": row["state"]}, 201)

        m = re.match(r"^/api/queue/([0-9a-f]{8,32})/cancel$", p)
        if m:
            r = qrow(m.group(1))
            if not r:
                return self.send_json({"error": "not found"}, 404)
            if r.get("state") in ("done", "error", "canceled"):
                return self.send_json({"state": r["state"]})
            if r.get("prompt_id"):
                try: comfy_api("/queue", {"delete": [r["prompt_id"]]}, timeout=15)
                except Exception: pass
                try:
                    with QLOCK:
                        cur = any(x.get("prompt_id") == r["prompt_id"] and x.get("state") == "running"
                                  for x in QROWS.values())
                    # 一定要帶 prompt_id。不帶就是全域中斷，當下在跑誰就停誰——ComfyUI 同時
                    # 服務好幾條路（I2VA Continue 的章節不在這張清單裡），實測砍過別人的工作。
                    if cur: comfy_api("/interrupt", {"prompt_id": r["prompt_id"]}, timeout=15)
                except Exception: pass
            qset(r["id"], state="canceled", t_end=int(time.time()))
            QWAKE.set()
            return self.send_json({"state": "canceled"})

        if p == "/api/matte/sam3":
            body = self.read_json()
            if not isinstance(body, dict):
                return self.send_json({"error": "empty body"}, 400)
            if getattr(self, "body_too_big", 0):
                return self.send_json({"error": "圖片太大（%.1f MB，上限 %d MB）"
                                       % (self.body_too_big / 1048576.0, MAX_BODY // 1048576)}, 413)
            if not sam3_ready():
                return self.send_json({"error": "SAM3 用不了：需要 ComfyUI 在跑、有 SAM3_Detect 節點，且 workflows/ 裡有那個工作流。"}, 501)
            t0 = time.time()
            try:
                png = sam3_mask(str(body.get("image") or ""),
                                str(body.get("prompt") or ""),
                                body.get("threshold") or 0.3)
            except SubmitError as e:
                slog("  [sam3] 失敗：%s" % e.msg)
                return self.send_json({"error": e.msg}, e.code)
            except Exception as e:
                slog("  [sam3] 失敗：%s" % str(e)[:160])
                return self.send_json({"error": str(e)[:200]}, 502)
            slog("  [sam3] 遮罩完成 %.1fs" % (time.time() - t0))
            return self.send_json({"png": png, "elapsed": round(time.time() - t0, 2)})
        if p == "/api/matte":
            body = self.read_json()
            if getattr(self, "body_too_big", 0):
                # 去背的輸入很容易是上一輪的全解析度 PNG，撞這條線很正常。
                # 回「empty body」的話完全看不出原因。
                return self.send_json({"error": "圖片太大（%.1f MB，上限 %d MB）"
                                       "——請用原圖去背，別拿去背結果再去一次"
                                       % (self.body_too_big / 1048576.0,
                                          MAX_BODY // 1048576)}, 413)
            if not isinstance(body, dict):
                return self.send_json({"error": "empty body"}, 400)
            why = cut_gpu_block()
            if why:
                slog("  [cut] 擋下：%s" % why)
                return self.send_json({"error": why + "——去背已暫停。"
                                                "要同時使用請到「系統設定 → 去背景」開啟。",
                                       "blocked": True}, 409)
            try:
                cut_ensure()
            except SubmitError as e:
                slog("  [cut] 服務拉不起來：%s" % e.msg)
                return self.send_json({"error": e.msg}, e.code)
            if not CUT_BUSY.acquire(blocking=False):
                slog("  [cut] 上一張還在跑，拒絕這一張")
                return self.send_json({"error": "上一張還在去背——同一張卡一次只做一張，"
                                                "等它完成再送。", "blocked": True}, 409)
            try:
                if not body.get("model"):
                    body["model"] = CONFIG.get("matte_model") or MATTE_DEFAULT
                req = urllib.request.Request(CUT_URL + "/matte", method="POST",
                                             data=json.dumps(body).encode("utf-8"),
                                             headers={"Content-Type": "application/json"})
                # 瀏覽器 120 秒就中止，而中止傳不到這裡。原本等 300 秒，
                # 等於每放棄一張就在 GPU 上多留三分鐘沒人認領的工作——
                # UI 已經顯示失敗了，機器還在忙。比瀏覽器早一點放手。
                with urllib.request.urlopen(req, timeout=110) as r:
                    return self.send_json(json.loads(r.read().decode("utf-8")))
            except urllib.error.HTTPError as e:
                try: return self.send_json(json.loads(e.read().decode("utf-8")), e.code)
                except Exception: return self.send_json({"error": "去背服務回應異常"}, 502)
            except Exception as e:
                return self.send_json({"error": str(e)[:200]}, 502)
            finally:
                CUT_BUSY.release()

        if p == "/api/cutout/segment":
            body = self.read_json()
            if getattr(self, "body_too_big", 0):
                # 去背的輸入很容易是上一輪的全解析度 PNG，撞這條線很正常。
                # 回「empty body」的話完全看不出原因。
                return self.send_json({"error": "圖片太大（%.1f MB，上限 %d MB）"
                                       "——請用原圖去背，別拿去背結果再去一次"
                                       % (self.body_too_big / 1048576.0,
                                          MAX_BODY // 1048576)}, 413)
            if not isinstance(body, dict):
                return self.send_json({"error": "empty body"}, 400)
            why = cut_gpu_block()
            if why:
                return self.send_json({"error": why + "——去背已暫停。"
                                                "要同時使用請到「系統設定 → 去背景」開啟。",
                                       "blocked": True}, 409)
            try:
                cut_ensure()
            except SubmitError as e:
                return self.send_json({"error": e.msg}, e.code)
            try:
                req = urllib.request.Request(CUT_URL + "/segment", method="POST",
                                             data=json.dumps(body).encode("utf-8"),
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=180) as r:
                    return self.send_json(json.loads(r.read().decode("utf-8")))
            except urllib.error.HTTPError as e:
                try: return self.send_json(json.loads(e.read().decode("utf-8")), e.code)
                except Exception: return self.send_json({"error": "去背服務回應異常"}, 502)
            except Exception as e:
                return self.send_json({"error": "去背服務連線失敗: %s" % str(e)[:200]}, 502)

        if p == "/api/comfy/run":
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            try:
                return self.send_json({"prompt_id": comfy_submit(body)})
            except SubmitError as e:
                return self.send_json({"error": e.msg}, e.code)

        if p == "/api/loras":
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            if not isinstance(body, dict):
                return self.send_json({"error": "body must be an object"}, 400)
            rid = str(body.get("id") or "")
            if rid and not ID_RE.match(rid):
                return self.send_json({"error": "bad id"}, 400)
            if not rid:
                rid = new_id()
            rec, err = norm_lora(body)
            if err:
                return self.send_json({"error": err}, 400)
            rec["id"] = rid
            rec["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            with LOCK:
                with open(os.path.join(LORAS, rid + ".json"), "w", encoding="utf-8") as f:
                    json.dump(rec, f, ensure_ascii=False, indent=1)
                rows = [r for r in load_lindex() if r.get("id") != rid]
                rows.append({"id": rid, "name": rec["name"], "main": rec["main"],
                             "nsub": len(rec["subs"]), "ts": rec["ts"]})
                rows.sort(key=lambda r: r.get("name", ""))
                save_lindex(rows)
            return self.send_json({"id": rid, "ts": rec["ts"]})

        if p == "/api/prompts":
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            if not isinstance(body, dict):
                return self.send_json({"error": "body must be an object"}, 400)
            rid = str(body.get("id") or "")
            if rid and not ID_RE.match(rid):
                return self.send_json({"error": "bad id"}, 400)
            if not rid:
                rid = new_id()
            mode = str(body.get("mode", "all")).strip()
            if mode not in MODES + ("all",):
                mode = "all"
            rec = {"id": rid,
                   "name": (str(body.get("name", "")).strip() or "未命名")[:80],
                   "text": str(body.get("text", "")),
                   "mode": mode,
                   "overlay": bool(body.get("overlay")),
                   "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
            with LOCK:
                with open(os.path.join(PROMPTS, rid + ".json"), "w", encoding="utf-8") as f:
                    json.dump(rec, f, ensure_ascii=False, indent=1)
                rows = [r for r in load_pindex() if r.get("id") != rid]
                rows.append({"id": rid, "name": rec["name"], "ts": rec["ts"],
                             "len": len(rec["text"]), "mode": mode, "overlay": rec["overlay"]})
                rows.sort(key=lambda r: r.get("name", ""))
                save_pindex(rows)
            return self.send_json({"id": rid, "ts": rec["ts"]})

        if p != "/api/history":
            return self.send_json({"error": "unknown endpoint"}, 404)

        try:
            body = self.read_json()
        except Exception as e:
            return self.send_json({"error": "bad json: %s" % e}, 400)
        if not isinstance(body, dict):
            return self.send_json({"error": "body must be an object"}, 400)

        rid = new_id()
        img_bytes = {}
        for key, suffix in (("thumb", ".jpg"), ("full", ".full.jpg")):
            data = body.pop(key, "") or ""
            if not data.startswith("data:image"):
                continue
            try:
                blob = base64.b64decode(data.split(",", 1)[1])
                img_bytes[key] = blob
                with open(os.path.join(HIST, rid + suffix), "wb") as f:
                    f.write(blob)
            except Exception:
                pass
        # full-resolution original for ComfyUI: sent inline ("orig" data URL) or copied from another
        # record ("orig_from": re-runs) / from the upload library ("orig_upload": imports)
        orig_bytes, orig_ext = parse_data_image(body.pop("orig", "") or "")
        if orig_bytes is None:
            src = None
            of = str(body.pop("orig_from", "") or "")
            ou = str(body.pop("orig_upload", "") or "")
            if of and ID_RE.match(of):
                src = orig_path(HIST, of)
            elif ou and re.match(r"^[0-9a-f]{40}$", ou):
                src = orig_path(UPLOADS, ou)
            if src and src[0]:
                try:
                    orig_bytes, orig_ext = open(src[0], "rb").read(), src[1]
                except OSError:
                    orig_bytes, orig_ext = None, ""
        if orig_bytes and orig_ext in ORIG_EXTS:
            try:
                with open(os.path.join(HIST, rid + ".orig." + orig_ext), "wb") as f:
                    f.write(orig_bytes)
            except Exception:
                orig_bytes, orig_ext = None, ""
        else:
            orig_bytes, orig_ext = None, ""
        # link the record to the upload library (idempotent by content hash)
        upload_id = ""
        if img_bytes.get("full"):
            try:
                upload_id = upload_register(img_bytes["full"], img_bytes.get("thumb", b""), str(body.get("image", "")),
                                            orig_bytes or b"", orig_ext)
            except Exception:
                upload_id = ""

        # extra images beyond the primary one (FL2VA last frame / REF2VA references), client-compressed JPEG
        more_n = 0
        for durl in (body.pop("more", None) or [])[:8]:
            mb, _ = parse_data_image(durl)
            if mb:
                more_n += 1
                try:
                    with open(os.path.join(HIST, "%s.x%d.jpg" % (rid, more_n)), "wb") as f:
                        f.write(mb)
                except Exception:
                    more_n -= 1

        # REF2VA 參考音訊：檔案存成 <rid>.a{n}.<ext>，描述與長度存進紀錄。
        # 不存的話，之後從歷史重送會少掉音訊，但 prompt 裡的 <Audio N> 還在——
        # ComfyUI 不會報錯，只會悄悄算出一支不一樣的影片。
        aud_meta = []
        for a in (body.pop("auds", None) or [])[:MAX_REF_AUDIO]:
            if not isinstance(a, dict):
                continue
            ab, aext = parse_data_audio(a.get("b64"))
            if not ab:
                continue
            n = len(aud_meta) + 1
            try:
                with open(os.path.join(HIST, "%s.a%d.%s" % (rid, n, aext)), "wb") as f:
                    f.write(ab)
            except Exception:
                continue
            aud_meta.append({"n": n, "ext": aext, "name": str(a.get("name") or "")[:120],
                             "dur": a.get("dur"), "desc": str(a.get("desc") or "")[:500]})

        rec = {
            "id": rid,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "nmore": more_n,
            "naudio": len(aud_meta),
            "audio": aud_meta,
            "image": str(body.get("image", ""))[:200],
            "dur": str(body.get("dur", ""))[:40],
            "state": str(body.get("state", ""))[:20],
            "elapsed_s": body.get("elapsed_s"),
            "usage": body.get("usage") or {},
            "errors": body.get("errors") or [],
            "warnings": body.get("warnings") or [],
            "content": str(body.get("content", "")),
            "raw": str(body.get("raw", "")),
            "zh": str(body.get("zh", "") or ""),
            "note": str(body.get("note", "") or "")[:4000],
            "attempts": max(1, int(body.get("attempts") or 1)) if str(body.get("attempts") or "1").isdigit() else 1,
            "shots": int(body.get("shots") or 0) if str(body.get("shots") or "0").isdigit() else 0,
            "sp_hash": str(body.get("sp_hash", ""))[:16],
            # T2VA：原始輸入劇情與 AI 潤飾後劇本
            "story_raw": str(body.get("story_raw", "") or "")[:8000],
            "story_polished": str(body.get("story_polished", "") or "")[:12000],
            # ---- generation context, so a record can be re-run exactly as it was made ----
            "mode": (str(body.get("mode", "i2va")) if str(body.get("mode", "i2va")) in MODES else "i2va"),
            "prompt_id": str(body.get("prompt_id", "") or "")[:64],
            "prompt_name": str(body.get("prompt_name", "") or "")[:80],
            "recipe": str(body.get("recipe", "") or "")[:40],
            # lora = {preset_id, preset_name, main, subs:[{key,gloss}], forced:[...], report:{...}}  (full snapshot)
            "lora": (body.get("lora") if isinstance(body.get("lora"), dict) else None),
            "upload_id": upload_id,
            "orig_ext": orig_ext,          # "" = only the 1024px working copy exists
        }
        with LOCK:
            with open(os.path.join(HIST, rid + ".json"), "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=1)
            rows = load_index()
            rows.insert(0, {k: rec[k] for k in
                            ("id", "ts", "image", "dur", "state", "elapsed_s", "mode", "upload_id",
                             "nmore", "naudio")}
                        | {"rating": ""}
                        | {"nerr": len(rec["errors"]), "nwarn": len(rec["warnings"]),
                           "full": os.path.exists(os.path.join(HIST, rid + ".full.jpg")),
                           "orig_ext": orig_ext,
                           "lora": ((rec["lora"] or {}).get("preset_name") or "") if rec["lora"] else "",
                           "prompt": rec["prompt_name"]})
            save_index(rows)
        return self.send_json({"id": rid})

    def do_DELETE(self):
        pth = urlparse(self.path).path
        m = re.match(r"^/api/media/file/(.+)$", pth)
        if m:
            fp = media_path(unquote(m.group(1)))
            if not fp or not os.path.isfile(fp):
                return self.send_json({"error": "not found"}, 404)
            try:
                os.remove(fp)
            except OSError as e:
                return self.send_json({"error": str(e)}, 500)
            return self.send_json({"ok": True})
        m = re.match(r"^/api/continues/([^/]+)$", pth)
        if m and ID_RE.match(m.group(1)):
            rid = m.group(1)
            if ic_active(rid):
                return self.send_json({"error": "這筆正在排隊或生成中，請先停止"}, 409)
            with LOCK:
                import glob as _g
                for fp in _g.glob(os.path.join(CONTINUES, rid + ".*")):
                    try:
                        os.remove(fp)
                    except OSError:
                        pass
                _atomic_json(CINDEX, [r for r in load_cindex() if r.get("id") != rid])
            return self.send_json({"ok": True})

        m = re.match(r"^/api/movies/([^/]+)$", pth)
        if m and ID_RE.match(m.group(1)):
            rid = m.group(1)
            with LOCK:
                import glob as _g
                for fp in _g.glob(os.path.join(MOVIES, rid + ".*")):
                    try:
                        os.remove(fp)
                    except OSError:
                        pass
                save_mindex([r for r in load_mindex() if r.get("id") != rid])
            return self.send_json({"ok": True})

        m = re.match(r"^/api/uploads/([0-9a-f]{40})$", pth)
        if m:
            h = m.group(1)
            cascade = parse_qs(urlparse(self.path).query).get("cascade", ["0"])[0] in ("1", "true", "yes")
            victims = []
            with LOCK:
                if cascade:
                    # every history record that references this upload goes too
                    rows = load_index()
                    victims = [r["id"] for r in rows if r.get("upload_id") == h]
                    for rid in victims:
                        hist_purge(rid)
                    if victims:
                        save_index([r for r in rows if r.get("upload_id") != h])
                # else: unlink only. History records keep their own image copy AND their upload_id, so they
                # still display / re-run fine, and if the same image is uploaded again (same content hash)
                # they re-attach to the new library entry automatically.
                for ext in (".jpg", ".thumb.jpg", ".orig.jpg", ".orig.png", ".orig.webp"):
                    try: os.remove(os.path.join(UPLOADS, h + ext))
                    except OSError: pass
                save_uindex([r for r in load_uindex() if r.get("id") != h])
            return self.send_json({"ok": True, "cascade": cascade, "deleted_history": len(victims)})

        m = re.match(r"^/api/lessons/([^/]+)$", pth)
        if m:
            rid = unquote(m.group(1))
            with LOCK:
                rows = load_lessons()
                n0 = len(rows)
                rows = [x for x in rows if x.get("id") != rid]
                if len(rows) < n0:
                    save_lessons(rows)
            return self.send_json({"ok": True})

        m = re.match(r"^/api/loras/([^/]+)$", pth)
        if m:
            rid = unquote(m.group(1))
            if not ID_RE.match(rid):
                return self.send_json({"error": "bad id"}, 400)
            with LOCK:
                try: os.remove(os.path.join(LORAS, rid + ".json"))
                except OSError: pass
                save_lindex([r for r in load_lindex() if r.get("id") != rid])
            return self.send_json({"ok": True})

        m = re.match(r"^/api/queue/([0-9a-f]{8,32})$", pth)
        if m:
            jid = m.group(1)
            with QLOCK:
                r = QROWS.get(jid)
                if not r:
                    return self.send_json({"error": "not found"}, 404)
                if r.get("state") in ("queued", "submitting", "running"):
                    return self.send_json({"error": "任務還在進行中，請先取消"}, 409)
                QROWS.pop(jid, None); QLIVE.pop(jid, None); QPREV.pop(jid, None)
                try: os.remove(os.path.join(QUEUE, jid + ".json"))
                except OSError: pass
                qsave(r) if False else None
                with LOCK:
                    tmp = QINDEX + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(sorted([qcompact(x) for x in QROWS.values()],
                                         key=lambda x: x.get("ts") or 0, reverse=True)[:500],
                                  f, ensure_ascii=False)
                    os.replace(tmp, QINDEX)
            return self.send_json({"ok": True})

        m = re.match(r"^/api/prompts/([^/]+)$", pth)
        if m:
            rid = unquote(m.group(1))
            if not ID_RE.match(rid):
                return self.send_json({"error": "bad id"}, 400)
            with LOCK:
                try: os.remove(os.path.join(PROMPTS, rid + ".json"))
                except OSError: pass
                save_pindex([r for r in load_pindex() if r.get("id") != rid])
            return self.send_json({"ok": True})

        m = re.match(r"^/api/history/([^/]+)$", pth)
        if not m:
            return self.send_json({"error": "unknown endpoint"}, 404)
        rid = unquote(m.group(1))
        if not ID_RE.match(rid):
            return self.send_json({"error": "bad id"}, 400)
        with LOCK:
            hist_purge(rid)
            save_index([r for r in load_index() if r.get("id") != rid])
        return self.send_json({"ok": True})


def main():
    global MEDIA_ROOT, COMFY_URL, WS_CLIENT_ID
    ap = argparse.ArgumentParser(description="H3 Storyboard - MiniMax H3 prompt director")
    ap.add_argument("--version", action="version", version="%s v%s (%s, %s License)"
                    % (APP_NAME, APP_VERSION, APP_REPO_URL, APP_LICENSE))
    ap.add_argument("--bind", default=CONFIG["bind"])
    ap.add_argument("--port", type=int, default=CONFIG["port"])
    ap.add_argument("--llama", default=CONFIG["llama_url"], help="llama-server URL (vision model)")
    ap.add_argument("--comfy", default=CONFIG["comfy_url"], help="ComfyUI API URL")
    ap.add_argument("--media-root", default=CONFIG["media_root"], help="folder browsed by the media view")
    a = ap.parse_args()
    if a.port != 9998:
        WS_CLIENT_ID = "h3-webui-%d" % a.port
    CONFIG["llama_url"] = a.llama.rstrip("/")
    CONFIG["comfy_url"] = a.comfy.rstrip("/")
    MEDIA_ROOT = a.media_root
    COMFY_URL = CONFIG["comfy_url"]
    ensure()
    if os.path.abspath(MEDIA_ROOT) == os.path.abspath(CONFIG_DEFAULTS["media_root"]):
        # 預設的 output/ 是遠端 ComfyUI 成品的本機鏡像，第一次跑時還不存在。
        # 只建預設那個：使用者自己填的路徑不存在是設定錯，不該默默幫他建一個空的。
        os.makedirs(MEDIA_ROOT, exist_ok=True)
    try:
        n_bf = upload_backfill()
        if n_bf:
            print("uploads: backfilled %d history record(s) into the upload library" % n_bf)
    except Exception as e:
        print("uploads: backfill skipped: %s" % e)
    if not os.path.exists(os.path.join(ROOT, PAGE)):
        print("[ERROR] 找不到 %s" % PAGE); sys.exit(1)
    # 禁用 SO_REUSEADDR：Windows 上它允許同一個 port 被多個實例同時綁定，
    # 舊碼實例會偷接請求造成「改了程式卻沒生效」的假象——寧可第二個實例直接啟動失敗。
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        srv = ThreadingHTTPServer((a.bind, a.port), H)
    except OSError:
        print("[ERROR] port %d 已被占用——已有另一個 h3-server 在跑，請先關掉它再啟動。" % a.port)
        sys.exit(1)
    print("  %s v%s  ·  %s  ·  %s License" % (APP_NAME, APP_VERSION, APP_REPO_URL, APP_LICENSE))
    print("  服務位址 : http://%s:%d" % (a.bind, a.port))
    print("  llama    : %s" % CONFIG["llama_url"])
    print("  ComfyUI  : %s" % CONFIG["comfy_url"])
    print("  設定檔   : %s" % (CONFIG_PATH if os.path.exists(CONFIG_PATH) else "(無 config.json，用預設 / 參數)"))
    print("  紀錄存放 : %s   （目前 %d 筆）" % (HIST, len(load_index())))
    print("  提示詞庫 : %s   （目前 %d 組）" % (PROMPTS, len(load_pindex())))
    md = media_list(limit=1, remote=False)   # 啟動時不去拉遠端清單，那要好幾秒
    print("  媒體庫   : %s   （%s）" % (MEDIA_ROOT,
          ("%d 個檔案" % md["total"]) if md else "資料夾不存在"))
    print("  去背服務 : %s" % (CUT_EXTERNAL or ("%s（按需啟動，port %d）" % (CUT_DIR, CUT_PORT))))
    print("  去背權重 : %s   （%d 個）" % (MATTE_DIR, len(matte_models())))
    print("  按 Ctrl+C 停止")
    print("-" * 60)
    try:
        qload_all()
        threading.Thread(target=qworker, daemon=True).start()
        ic_boot()
        threading.Thread(target=icworker, daemon=True).start()
        threading.Thread(target=wsworker, daemon=True).start()
        print("  queue    : %d 筆任務，派工已啟動" % len(QROWS))
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服務已停止")


if __name__ == "__main__":
    main()
