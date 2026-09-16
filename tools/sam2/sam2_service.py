# -*- coding: utf-8 -*-
"""SAM2 點選去背服務（獨立環境，不碰 ComfyUI 的 .env）。

為什麼要獨立成一支程序：
  1) 它跟 ComfyUI 共用同一張 3090。不用時必須能把模型整個從 VRAM 卸掉，
     同程序的話做不到乾淨釋放。
  2) ComfyUI 的環境是整套流程的核心，不值得為了省 2GB 磁碟去冒 pip
     動到它 numpy/opencv 的風險。

行為：
  - 模型延遲載入，閒置 IDLE_UNLOAD 秒後自動卸掉並清空 CUDA 快取
  - 沒有任何請求超過 IDLE_EXIT 秒就整支結束，由 h3-server 下次再拉起來
  - 只聽 127.0.0.1
"""
import io, json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("SAM2_PORT", "9996"))
MODELS_DIR = os.environ.get("SAM2_MODELS", os.path.join(os.path.dirname(os.path.abspath(__file__)), "models"))
WEIGHT = os.environ.get("SAM2_WEIGHT", "sam2_b.pt")
IDLE_UNLOAD = float(os.environ.get("SAM2_IDLE_UNLOAD", "180"))   # 卸模型
IDLE_EXIT = float(os.environ.get("SAM2_IDLE_EXIT", "900"))       # 整支結束

_model = None
_lock = threading.RLock()
_last = time.time()


def _weight_path():
    os.makedirs(MODELS_DIR, exist_ok=True)
    return os.path.join(MODELS_DIR, WEIGHT)


def get_model():
    """延遲載入。ultralytics 首次會自動把權重下載到 _weight_path()。"""
    global _model
    with _lock:
        if _model is None:
            from ultralytics import SAM
            _model = SAM(_weight_path())
        return _model


def unload():
    global _model
    with _lock:
        if _model is None:
            return False
        _model = None
        try:
            import torch, gc
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        return True


def segment(img_bytes, points, labels, bbox=None):
    """points 是 [[x, y], ...] 的像素座標，labels 1=保留 0=排除。
    bbox 是 [x0, y0, x1, y1]（XYXY 像素），可單獨用、也可與點並用。
    回傳 (mask_bool_2d, h, w)。

    框會被 ultralytics 拆成兩個角點（labels 2/3）接在 points 前面
    （predict.py:778-783 的 torch.cat），所以框跟點必須在同一個 batch 維度：
    一個框配一組點 = 一個物件。框不是硬裁切，一樣是 soft embedding，
    但對「相鄰兩人選其一」的約束力遠強於負點。

    形狀很關鍵：ultralytics 的 predict.py 對扁平的 (N,2) 會做
        points, labels = points[:, None, :], labels[:, None]
    也就是把 N 個點當成「N 個各自獨立的單點提示」，產生 N 個互不相干的遮罩。
    那樣紅點只會變成自己那一張遮罩，聯集起來等於完全沒作用——ClipForge 沒做
    負向點，所以沒踩到這個。包成 (1, N, 2) 才是「同一個物件、N 個提示」，
    ndim==3 就不會被重塑，負向點才真的會把選區縮小。"""
    import numpy as np, cv2
    arr = np.frombuffer(img_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("圖片解不開")
    h, w = img.shape[:2]
    m = get_model()
    kw = {}
    if points:
        kw["points"] = [points]
        kw["labels"] = [labels]
    if bbox:
        kw["bboxes"] = [bbox]
    if not kw:
        raise ValueError("至少要有一個點或一個框")
    with _lock:
        res = m(img, verbose=False, **kw)
    if not res or res[0].masks is None:
        return np.zeros((h, w), dtype=bool), h, w
    data = res[0].masks.data
    try:
        mask = data.any(0).cpu().numpy()
    except Exception:
        mask = np.asarray(data).any(0)
    if mask.shape != (h, w):
        # 實測下 ultralytics 已經把遮罩還原成原圖尺寸（847x832 進、847x832 出），
        # 所以這裡平常不會跑。留著當安全網，並且用雙線性插值再取閾值，
        # 若哪天真的需要放大，邊界會落在次像素位置而不是方格化。
        mask = cv2.resize(mask.astype("float32"), (w, h), interpolation=cv2.INTER_LINEAR) > 0.5
    return mask.astype(bool), h, w


def _fill_holes(mask, max_px):
    """被前景包住、且面積小於 max_px 的背景塊補起來。
    沒用 scipy（這個環境沒裝），改用連通元件：碰到圖邊的背景塊是真的外部，
    不能填；沒碰到邊的就是洞。大洞保留，那通常是真的鎏空（手臂中間、髮縫）。"""
    import numpy as np, cv2
    if max_px <= 0:
        return mask
    inv = (~mask).astype("uint8")
    n, lab, st, _ = cv2.connectedComponentsWithStats(inv, 8)
    if n <= 1:
        return mask
    h, w = mask.shape
    fill = np.zeros(n, bool)
    for i in range(1, n):
        x, y, bw, bh, area = st[i]
        touches_edge = (x == 0 or y == 0 or x + bw >= w or y + bh >= h)
        fill[i] = (not touches_edge) and area <= max_px
    return mask | fill[lab]


def _drop_specks(mask, min_px):
    """與主體分離、面積小於 min_px 的前景碎屑丟掉。主體本身永遠保留，
    否則選到小物件時會整個被清掉。"""
    import numpy as np, cv2
    if min_px <= 0:
        return mask
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask.astype("uint8"), 8)
    if n <= 2:
        return mask
    areas = st[1:, cv2.CC_STAT_AREA]
    biggest = int(areas.argmax()) + 1
    keep = np.zeros(n, bool)
    for i in range(1, n):
        keep[i] = (i == biggest) or st[i, cv2.CC_STAT_AREA] >= min_px
    return keep[lab]


def _smooth(mask, sigma):
    """模糊再取閾值。磨掉鐘齒，面積幾乎不變（凸的削掉、凹的填平）。"""
    import cv2
    if sigma <= 0:
        return mask
    return cv2.GaussianBlur(mask.astype("float32"), (0, 0), sigma) > 0.5


# 精修強度：(洞上限, 碎屑下限, 平滑 sigma)，前兩項是影像面積的比例，
# 第三項以 800px 長邊為基準縮放，換張圖才不會強度跡。
REFINE_LEVELS = {
    0: (0.0,    0.0,    0.0),
    1: (0.002,  0.0002, 0.0),
    2: (0.005,  0.0005, 1.5),
    3: (0.02,   0.002,  3.0),
}


def refine_mask(mask, level):
    if not level:
        return mask
    hole_r, blob_r, sigma = REFINE_LEVELS.get(int(level), REFINE_LEVELS[2])
    h, w = mask.shape
    area = float(h * w)
    m = _fill_holes(mask, int(area * hole_r))
    m = _drop_specks(m, int(area * blob_r))
    m = _smooth(m, sigma * (max(h, w) / 800.0))
    # 平滑可能又開出小洞，再補一次（便宜）
    return _fill_holes(m, int(area * hole_r))


def cutout_png(img_bytes, mask, bg):
    """bg=None -> 透明去背；否則填上指定顏色（#rrggbb）。回傳 PNG bytes。"""
    import numpy as np, cv2
    arr = np.frombuffer(img_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if bg:
        c = bg.lstrip("#")
        rgb = tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
        out = img.copy()
        out[~mask] = (rgb[2], rgb[1], rgb[0])          # BGR
        ok, buf = cv2.imencode(".png", out)
    else:
        b, g, r = cv2.split(img)
        a = (mask.astype("uint8") * 255)
        ok, buf = cv2.imencode(".png", cv2.merge([b, g, r, a]))
    if not ok:
        raise ValueError("PNG 編碼失敗")
    return buf.tobytes()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        global _last
        _last = time.time()
        if self.path == "/health":
            return self._json({"ok": True, "loaded": _model is not None, "port": PORT})
        if self.path == "/unload":
            return self._json({"unloaded": unload()})
        return self._json({"error": "unknown endpoint"}, 404)

    def do_POST(self):
        global _last
        _last = time.time()
        if self.path != "/segment":
            return self._json({"error": "unknown endpoint"}, 404)
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 48 * 1024 * 1024:
            self.close_connection = True
            return self._json({"error": "body 太大或為空"}, 413)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception as e:
            return self._json({"error": "bad json: %s" % e}, 400)
        try:
            import base64
            durl = str(body.get("image") or "")
            raw = base64.b64decode(durl.split(",", 1)[1] if "," in durl else durl)
            pts = [[float(p[0]), float(p[1])] for p in (body.get("points") or [])]
            lbs = [int(x) for x in (body.get("labels") or [])]
            if len(pts) != len(lbs):
                return self._json({"error": "points/labels 數量對不上"}, 400)
            bb = body.get("bbox")
            if isinstance(bb, (list, tuple)) and len(bb) == 4:
                x0, y0, x1, y1 = (float(v) for v in bb)
                bb = [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
                if bb[2] - bb[0] < 2 or bb[3] - bb[1] < 2:
                    bb = None
            else:
                bb = None
            if not pts and not bb:
                return self._json({"error": "至少要有一個點或一個框"}, 400)
            lvl = body.get("refine")
            lvl = 2 if lvl is None else int(lvl)
            t0 = time.time()
            mask, h, w = segment(raw, pts, lbs, bb)
            before = float(mask.mean())
            mask = refine_mask(mask, lvl)
            png = cutout_png(raw, mask, body.get("bg"))
            import base64 as b64
            return self._json({"png": "data:image/png;base64," + b64.b64encode(png).decode(),
                               "w": w, "h": h, "covered": float(mask.mean()),
                               "covered_raw": before, "refine": lvl,
                               "elapsed": round(time.time() - t0, 2)})
        except Exception as e:
            return self._json({"error": str(e)[:300]}, 500)


def janitor():
    while True:
        time.sleep(10)
        idle = time.time() - _last
        if idle > IDLE_EXIT:
            os._exit(0)                     # h3-server 下次會重新拉起來
        if idle > IDLE_UNLOAD and _model is not None:
            unload()


if __name__ == "__main__":
    threading.Thread(target=janitor, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    sys.stderr.write("SAM2 service on 127.0.0.1:%d  models=%s%s" % (PORT, MODELS_DIR, chr(10)))
    sys.stderr.flush()
    srv.serve_forever()
