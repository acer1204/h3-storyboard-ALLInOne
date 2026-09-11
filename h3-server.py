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
import argparse, base64, hashlib, io, json, os, re, socket, struct, subprocess, sys, threading, time
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, unquote, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
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
    """Persist the editable keys to config.json and refresh module globals."""
    global MEDIA_ROOT, COMFY_URL
    MEDIA_ROOT = CONFIG["media_root"]
    COMFY_URL = CONFIG["comfy_url"].rstrip("/")
    keep = {k: CONFIG[k] for k in CONFIG_DEFAULTS if k not in ("bind", "port")}
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
    """True if ComfyUI is generating or has queued jobs; None if unreachable."""
    try:
        q = comfy_api("/queue", timeout=6)
        return bool(q.get("queue_running")) or bool(q.get("queue_pending"))
    except Exception:
        return None


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
    for t in times:
        try:
            fr = sp.run([ff, "-ss", str(t), "-i", ap, "-frames:v", "1",
                         "-vf", "scale=448:-2", "-q:v", "7", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                        capture_output=True, timeout=30)
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


def movie_concat(rel_files):
    """把多段 mp4（media_root 相對路徑）用 ffmpeg concat demuxer 無損串接成一部長片。
    回傳輸出檔的 media_root 相對路徑。"""
    import subprocess as sp
    import tempfile
    ff = find_ffmpeg()
    if not ff:
        raise ValueError("找不到 ffmpeg（PATH 或 ComfyUI 環境內都沒有）")
    abses = []
    for rf in rel_files:
        ap = os.path.abspath(os.path.join(MEDIA_ROOT, rf.replace("\\", "/")))
        if not ap.startswith(os.path.abspath(MEDIA_ROOT)) or not os.path.exists(ap):
            raise ValueError("片段不存在或路徑不合法: %s" % rf)
        abses.append(ap)
    if len(abses) < 2:
        raise ValueError("至少要兩段影片")
    outdir = os.path.join(MEDIA_ROOT, "video", "movies")
    os.makedirs(outdir, exist_ok=True)
    # 容器要跟片段一致：把 WebM(VP9/Opus) 用 -c copy 塞進 .mp4 會失敗
    _ext = os.path.splitext(abses[0])[1].lower() or ".mp4"
    if _ext not in VIDEO_EXT: _ext = ".mp4"
    out = os.path.join(outdir, "movie_%s_%dseg%s" % (time.strftime("%Y%m%d_%H%M%S"), len(abses), _ext))
    lst = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    try:
        for ap in abses:
            lst.write("file '%s'\n" % ap.replace("'", "'\\''"))
        lst.close()
        r = sp.run([ff, "-y", "-f", "concat", "-safe", "0", "-i", lst.name, "-c", "copy", out],
                   capture_output=True, timeout=300)
        if r.returncode != 0:
            raise ValueError("ffmpeg 失敗: " + r.stderr.decode("utf-8", "replace")[-400:])
    finally:
        try:
            os.unlink(lst.name)
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
LOCK = threading.Lock()
# QLOCK 保護記憶體內的佇列快取／進度／預覽；LOCK 仍只保護磁碟索引的讀改寫。
# 規則：需要兩者時一律先 QLOCK 再 LOCK；兩者都不得在呼叫 ComfyUI 期間持有。
QLOCK = threading.RLock()
QROWS = {}   # jid -> row（磁碟的記憶體鏡像）
QLIVE = {}   # jid -> {progress, stage, prev_step}（僅記憶體，重啟即失）
QPREV = {}   # jid -> 預覽影像 bytes（僅記憶體）
COMFY_LAST_STATE = {}   # prompt_id -> last state string, so the console logs transitions, not every poll


def comfy_note(pid, state, extra=""):
    """Print one line when a ComfyUI job changes state (queued -> running -> done/error)."""
    key = state + ("|" + extra if extra else "")
    if COMFY_LAST_STATE.get(pid) != key:
        COMFY_LAST_STATE[pid] = key
        sys.stderr.write("  comfy %s : %s%s\n" % (pid[:8], state, (" " + extra) if extra else ""))
        if state in ("done", "error"):
            COMFY_LAST_STATE.pop(pid, None)
MAX_BODY = 24 * 1024 * 1024
MV_MAX_IMAGES = 24            # FL2VA Movie 分鏡張數上限；N 張圖 = N-1 個片段
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


def media_list(limit=1000):
    rows = []
    root = os.path.realpath(MEDIA_ROOT)
    if not os.path.isdir(root):
        return None
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
            rows.append({"path": os.path.relpath(fp, root).replace(os.sep, "/"),
                         "name": name, "size": st.st_size,
                         "mtime": int(st.st_mtime),
                         "kind": mime.split("/")[0], "mime": mime})
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    total = len(rows)
    return {"total": total, "truncated": total > limit, "files": rows[:limit]}


def comfy_api(path, data=None, timeout=30):
    import urllib.request as _u
    maybe_reload_config()
    req = _u.Request(COMFY_URL + path,
                     data=json.dumps(data).encode() if data is not None else None,
                     headers={"Content-Type": "application/json"} if data is not None else {})
    return json.loads(_u.urlopen(req, timeout=timeout).read() or b"{}")


def llama_api(path, data=None, timeout=600):
    """代理 llama-server：位址只存在 config.json，瀏覽器一律走同源 /api/llama/*。"""
    import urllib.request as _u
    base = CONFIG["llama_url"].rstrip("/")
    req = _u.Request(base + path,
                     data=json.dumps(data).encode() if data is not None else None,
                     headers={"Content-Type": "application/json"} if data is not None else {})
    return json.loads(_u.urlopen(req, timeout=timeout).read() or b"{}")


def comfy_upload(name, blob):
    """multipart 上傳圖片到 ComfyUI input 資料夾"""
    import urllib.request as _u
    bnd = "----h3webui%d" % int(time.time() * 1000)
    body = io.BytesIO()
    def w(t): body.write(t if isinstance(t, bytes) else t.encode())
    w(f"--{bnd}\r\n")
    w(f'Content-Disposition: form-data; name="image"; filename="{name}"\r\n')
    w("Content-Type: image/jpeg\r\n\r\n"); w(blob); w("\r\n")
    w(f"--{bnd}\r\n")
    w('Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n')
    w(f"--{bnd}--\r\n")
    req = _u.Request(COMFY_URL + "/upload/image", data=body.getvalue(),
                     headers={"Content-Type": "multipart/form-data; boundary=" + bnd})
    return json.loads(_u.urlopen(req, timeout=60).read())


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
        except Exception:
            pass
    if blob is None:
        blob, ext2 = parse_data_image(body.get("image"))
        if blob is not None:
            ext = ext2
    for durl in (body.get("more") or [])[:8]:
        mb, _ = parse_data_image(durl)
        if mb:
            extra_blobs.append(mb)
    if blob is None and run_mode != "t2va":
        raise SubmitError("沒有可用的圖片（rec_id 找不到原圖，也沒帶 image）", 400)
    dur = None
    md = re.search(r"\d+", str(body.get("dur") or ""))
    if md:
        dur = int(md.group(0))
    try:
        up_name = None
        if blob is not None:
            name = "h3webui_%s.%s" % (new_id(), ext)
            up_name = comfy_upload(name, blob).get("name", name)
        extra_names = []
        for k2, mb in enumerate(extra_blobs):
            nm2 = "h3webui_%s_x%d.jpg" % (new_id(), k2 + 1)
            extra_names.append(comfy_upload(nm2, mb).get("name", nm2))
        flds = split_fields(full_prompt, run_mode)
        graph, extra = comfy_build(str(body.get("imd") or flds["imd"]),
                                   str(body.get("soundscape") or flds["soundscape"]),
                                   str(body.get("music") or flds["music"]),
                                   up_name, dur,
                                   body.get("wf"), blob,
                                   mode=run_mode, extra_names=extra_names, full_prompt=full_prompt)
        payload = {"prompt": graph, "client_id": "h3-webui"}
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


def qenqueue(payload, mode="", title="", rec_id="", idem=""):
    """建立一筆 queued 任務並喚醒派工。回傳新列。"""
    with QLOCK:
        if idem:
            for r in QROWS.values():
                if r.get("idem") == idem:
                    return dict(r)          # 同一次點擊重送：回傳既有列，不重複算
        jid = new_id()
        row = {"id": jid, "ts": int(time.time()), "state": "queued",
               "mode": mode or str(payload.get("mode") or ""), "title": title[:120],
               "rec_id": rec_id, "idem": idem, "prompt_id": "",
               "payload": payload, "video": "", "first_frame": "", "error": "",
               "t_submit": 0, "t_end": 0}
        qsave(row)
    QWAKE.set()
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



# ---------------------------------------------------------------- comfy websocket
# ComfyUI 的進度與預覽事件是「單播給單一 clientId」的：同一個 id 開第二條連線，
# 第一條會靜默收不到東西。所以整台機器只由伺服器開這一條，再分發給所有瀏覽器。
# clientId 必須與送單時用的 client_id 一致，否則收不到自己送的任務的事件。
WS_CLIENT_ID = "h3-webui"


def _ws_connect():
    import base64, hashlib
    u = urlparse(CONFIG["comfy_url"])
    host = u.hostname or "127.0.0.1"
    port = u.port or (443 if u.scheme == "https" else 80)
    key = base64.b64encode(os.urandom(16)).decode()
    req = ("GET /ws?clientId=%s HTTP/1.1" % WS_CLIENT_ID,
           "Host: %s:%d" % (host, port), "Upgrade: websocket", "Connection: Upgrade",
           "Sec-WebSocket-Key: " + key, "Sec-WebSocket-Version: 13", "", "")
    sock = socket.create_connection((host, port), timeout=15)
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


def _ws_frames(sock, rest):
    """產出 (opcode, payload)。只處理伺服器->用戶端方向（不會有 mask）。"""
    buf = rest
    while True:
        while len(buf) < 2:
            d = sock.recv(65536)
            if not d:
                return
            buf += d
        b0, b1 = buf[0], buf[1]
        op = b0 & 0x0F
        ln = b1 & 0x7F
        i = 2
        if ln == 126:
            while len(buf) < 4:
                buf += sock.recv(65536)
            ln = struct.unpack(">H", buf[2:4])[0]; i = 4
        elif ln == 127:
            while len(buf) < 10:
                buf += sock.recv(65536)
            ln = struct.unpack(">Q", buf[2:10])[0]; i = 10
        while len(buf) < i + ln:
            d = sock.recv(65536)
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
                    if t == "executing":
                        cur = _ws_job_for(d.get("prompt_id")) or cur
                        if d.get("node") is None and cur:
                            with QLOCK:
                                QLIVE.pop(cur, None)
                            cur = None
                    elif t == "progress":
                        jid = _ws_job_for(d.get("prompt_id")) or cur
                        mx = d.get("max") or 0
                        if jid and mx:
                            with QLOCK:
                                QLIVE[jid] = {"progress": min(1.0, (d.get("value") or 0) / mx),
                                              "stage": str(d.get("node") or "")}
                    elif t == "kj_preview_override":
                        # ModelPreviewOverrideKJ 的即時預覽：base64，mime 可能是 video/mp4 或 image/webp
                        jid = cur or _ws_job_for(None)
                        b64 = d.get("image")
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
                elif op == 2 and cur:
                    # ComfyUI 內建的二進位預覽：前 8 bytes 是事件型別與影像格式
                    if len(payload) > 8:
                        with QLOCK:
                            QPREV[cur] = ("image/jpeg", payload[8:])
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
                mode=None, extra_names=None, full_prompt=None):
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
        tl["items"] = [it for it in tl.get("items", []) if it.get("type") != "image"]
        done = True
    elif dmode:
        # 重建圖片清單：非圖片項目（音訊參考等）保留原樣
        keep = [it for it in tl.get("items", []) if it.get("type") != "image"]
        img_items = []
        for k, nm in enumerate(names):
            item = {"id": "h3web-img-%d" % (k + 1), "type": "image", "value": nm,
                    "order": k, "enabled": True, "thumbnail": None}
            if dmode == "FL2VA":
                item["slot"] = k          # 0 = 首幀, 1 = 尾幀
            img_items.append(item)
        tl["items"] = img_items + keep
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

    extra = dict(extra)
    extra["client_id"] = "h3-webui"
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
            fp = media_path(unquote(m.group(1)))
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
        if pth.startswith("/api/comfy/status/"):
            return
        sys.stderr.write("  %s %s\n" % (self.command, pth))

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
        if n <= 0 or n > MAX_BODY:
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
            pid = m.group(1)
            try:
                hist = comfy_api("/history/" + pid, timeout=15)
            except Exception as e:
                return self.send_json({"error": "ComfyUI 連不上: %s" % e}, 502)
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
                state = "done" if st.get("completed") else ("error" if st.get("status_str") == "error" else "running")
                comfy_note(pid, state, (outs[-1] if (state == "done" and outs) else (err[:80] if err else "")))
                return self.send_json({"state": state, "outputs": outs, "error": err})
            try:
                q = comfy_api("/queue", timeout=15)
            except Exception as e:
                return self.send_json({"error": "ComfyUI 連不上: %s" % e}, 502)
            for item in q.get("queue_running", []):
                if len(item) > 1 and item[1] == pid:
                    comfy_note(pid, "running")
                    return self.send_json({"state": "running"})
            for i, item in enumerate(q.get("queue_pending", [])):
                if len(item) > 1 and item[1] == pid:
                    comfy_note(pid, "queued", "(pos %d)" % (i + 1))
                    return self.send_json({"state": "queued", "pos": i + 1})
            comfy_note(pid, "unknown")
            return self.send_json({"state": "unknown"})

        if p == "/api/media":
            data = media_list()
            if data is None:
                return self.send_json({"error": "媒體資料夾不存在: " + MEDIA_ROOT}, 404)
            return self.send_json(data)

        m = re.match(r"^/api/media/file/(.+)$", p)
        if m:
            fp = media_path(unquote(m.group(1)))
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
                return self.send_json({"error": str(e)}, 502)

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
                return self.send_json(llama_api("/v1/chat/completions", body, timeout=600))
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

        if p == "/api/movie/concat":
            try:
                body = self.read_json()
            except Exception as e:
                return self.send_json({"error": "bad json: %s" % e}, 400)
            files = [str(x) for x in (body.get("files") or []) if str(x).strip()]
            try:
                out = movie_concat(files)
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
            target = str(body.get("target") or "")
            thr = int(CONFIG.get("gpu_free_mb", 4000))
            mem = gpu_mem()
            used = mem.get("used_mb")
            if used is None:
                return self.send_json({"ready": True, "state": "no_gpu_info", "gpu": mem})
            in_flight = LLAMA_INFLIGHT[0] > 0
            since_llama = (time.time() - LLAMA_LAST[0]) if LLAMA_LAST[0] else 1e9
            if target == "llama":
                if in_flight:
                    # 另一個 llama 呼叫正在跑：同對象直接放行
                    return self.send_json({"ready": True, "state": "llama_resident", "gpu": mem})
                if since_llama < 8 and not comfy_busy():
                    # 權重還在卡上（idle 卸載前）且 ComfyUI 沒在算圖：連續呼叫不用等
                    return self.send_json({"ready": True, "state": "llama_resident", "gpu": mem})
                if used <= thr:
                    return self.send_json({"ready": True, "state": "gpu_free", "gpu": mem})
                busy = comfy_busy()
                if busy:
                    return self.send_json({"ready": False, "state": "comfy_busy", "gpu": mem})
                try:
                    comfy_free()
                    state = "freeing_comfy"
                except Exception as e:
                    state = "comfy_unreachable: %s" % e
                time.sleep(1.5)
                mem = gpu_mem()
                return self.send_json({"ready": (mem.get("used_mb") or 0) <= thr, "state": state, "gpu": mem})
            if target == "comfy":
                if in_flight:
                    return self.send_json({"ready": False, "state": "llama_in_use", "gpu": mem})
                if used <= thr:
                    return self.send_json({"ready": True, "state": "gpu_free", "gpu": mem})
                if comfy_busy():
                    # 高 VRAM 是 ComfyUI 自己在用：直接排進它的佇列即可
                    return self.send_json({"ready": True, "state": "comfy_owns_gpu", "gpu": mem})
                if since_llama < 90:
                    return self.send_json({"ready": False, "state": "waiting_llama_unload", "gpu": mem})
                # 沒人聲稱佔用：多半是 ComfyUI 閒置駐留的模型，送單無妨
                return self.send_json({"ready": True, "state": "assume_comfy_resident", "gpu": mem})
            return self.send_json({"error": "target 必須是 llama 或 comfy"}, 400)

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
                    if cur: comfy_api("/interrupt", {}, timeout=15)
                except Exception: pass
            qset(r["id"], state="canceled", t_end=int(time.time()))
            QWAKE.set()
            return self.send_json({"state": "canceled"})

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

        rec = {
            "id": rid,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "nmore": more_n,
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
                            ("id", "ts", "image", "dur", "state", "elapsed_s", "mode", "upload_id", "nmore")}
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
                        for ext in (".json", ".jpg", ".full.jpg", ".orig.jpg", ".orig.png", ".orig.webp"):
                            try: os.remove(os.path.join(HIST, rid + ext))
                            except OSError: pass
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
            for ext in (".json", ".jpg", ".full.jpg", ".orig.jpg", ".orig.png", ".orig.webp"):
                try: os.remove(os.path.join(HIST, rid + ext))
                except OSError: pass
            save_index([r for r in load_index() if r.get("id") != rid])
        return self.send_json({"ok": True})


def main():
    global MEDIA_ROOT, COMFY_URL
    ap = argparse.ArgumentParser(description="H3 Storyboard - MiniMax H3 prompt director")
    ap.add_argument("--bind", default=CONFIG["bind"])
    ap.add_argument("--port", type=int, default=CONFIG["port"])
    ap.add_argument("--llama", default=CONFIG["llama_url"], help="llama-server URL (vision model)")
    ap.add_argument("--comfy", default=CONFIG["comfy_url"], help="ComfyUI API URL")
    ap.add_argument("--media-root", default=CONFIG["media_root"], help="folder browsed by the media view")
    a = ap.parse_args()
    CONFIG["llama_url"] = a.llama.rstrip("/")
    CONFIG["comfy_url"] = a.comfy.rstrip("/")
    MEDIA_ROOT = a.media_root
    COMFY_URL = CONFIG["comfy_url"]
    ensure()
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
    print("  服務位址 : http://%s:%d" % (a.bind, a.port))
    print("  llama    : %s" % CONFIG["llama_url"])
    print("  ComfyUI  : %s" % CONFIG["comfy_url"])
    print("  設定檔   : %s" % (CONFIG_PATH if os.path.exists(CONFIG_PATH) else "(無 config.json，用預設 / 參數)"))
    print("  紀錄存放 : %s   （目前 %d 筆）" % (HIST, len(load_index())))
    print("  提示詞庫 : %s   （目前 %d 組）" % (PROMPTS, len(load_pindex())))
    md = media_list(limit=1)
    print("  媒體庫   : %s   （%s）" % (MEDIA_ROOT,
          ("%d 個檔案" % md["total"]) if md else "資料夾不存在"))
    print("  按 Ctrl+C 停止")
    print("-" * 60)
    try:
        qload_all()
        threading.Thread(target=qworker, daemon=True).start()
        threading.Thread(target=wsworker, daemon=True).start()
        print("  queue    : %d 筆任務，派工已啟動" % len(QROWS))
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服務已停止")


if __name__ == "__main__":
    main()
