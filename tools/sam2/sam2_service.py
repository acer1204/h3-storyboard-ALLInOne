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
# BiRefNet 權重直接沿用 ComfyUI 那份，不再複製一份占磁碟
BREF_DIR = os.environ.get("BREF_MODELS",
                          "E:/ComfyUI-MiniMaxH3/ComfyUI/models/background_removal")
BREF_DEFAULT = os.environ.get("BREF_WEIGHT", "birefnet.safetensors")

_model = None
_bref = None            # (模型物件, 權重檔名)
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
    global _model, _bref
    with _lock:
        if _model is None and _bref is None:
            return False
        _model = None
        _bref = None
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


# ---------------------------------------------------------------- BiRefNet

def bref_list():
    """可用的去背模型。image_size 每個模型自己帶，因為 HR 版本是在
    2048 訓練的，跑 1024 等於浪費掉它的優勢（ComfyUI 那邊就是鎖死 1024）。"""
    out = []
    if not os.path.isdir(BREF_DIR):
        return out
    for fn in sorted(os.listdir(BREF_DIR)):
        if not fn.endswith(".safetensors"):
            continue
        low = fn.lower()
        size = 2048 if ("hr" in low or "2048" in low) else 1024
        out.append({"name": fn,
                    "mb": round(os.path.getsize(os.path.join(BREF_DIR, fn)) / 1048576.0),
                    "image_size": size})
    return out


def get_bref(name):
    """延遲載入 / 換模型。一次只留一個在 VRAM 裡。"""
    global _bref
    import torch
    from safetensors.torch import load_file
    with _lock:
        if _bref is not None and _bref[1] == name:
            return _bref[0]
        _bref = None
        gc_cuda()
        from birefnet import BiRefNet
        fp = os.path.join(BREF_DIR, name)
        if not os.path.exists(fp):
            raise ValueError("找不到模型：" + name)
        m = BiRefNet()
        missing, unexpected = m.load_state_dict(load_file(fp), strict=False)
        if missing:
            # strict=False 不會报錯，但權重尺寸對不上時會静默吐垃圾，寧可在這裡死
            raise ValueError("權重跟模型架構對不上（缺 %d 個張量），"
                             "可能是 lite/Swin-T 版本，目前只支援 Swin-L。" % len(missing))
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        m = m.to(device=dev, dtype=torch.float16 if dev == "cuda" else torch.float32).eval()
        _bref = (m, name)
        return m


def gc_cuda():
    try:
        import torch, gc
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def matte(img_bytes, model_name, image_size=None):
    """回傳 float32 的軟 alpha（0..1，跟原圖同尺寸）。
    前處理跟 ComfyUI 一致：mean=0 std=1、不裁切，所以就是 resize 到正方形。"""
    import numpy as np, cv2, torch
    arr = np.frombuffer(img_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("圖片解不開")
    h, w = img.shape[:2]
    m = get_bref(model_name)
    if image_size is None:
        image_size = next((x["image_size"] for x in bref_list() if x["name"] == model_name), 1024)
    dev = next(m.parameters()).device
    dt = next(m.parameters()).dtype
    x = torch.from_numpy(img[:, :, ::-1].copy()).to(dev).float().div(255.0).unsqueeze(0).movedim(-1, 1)
    x = torch.nn.functional.interpolate(x, size=(image_size, image_size), mode="bicubic", antialias=True)
    with _lock:
        with torch.no_grad():
            out = m(pixel_values=x.to(dt))
    out = torch.nn.functional.interpolate(out.float(), size=(h, w), mode="bicubic", antialias=False)
    return out.sigmoid()[0, 0].cpu().numpy().clip(0.0, 1.0), h, w


def matte_png(img_bytes, alpha, bg):
    """軟 alpha 合成。bg=None -> 透明；否則混到底色。
    軟邊是重點，不能先取閾值，面紗那種半透明就是靠這個才對。"""
    import numpy as np, cv2
    img = cv2.imdecode(np.frombuffer(img_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    a = alpha[:, :, None].astype(np.float32)
    if bg:
        c = bg.lstrip("#")
        rgb = tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
        back = np.zeros_like(img, np.float32)
        back[:, :, 0] = rgb[2]; back[:, :, 1] = rgb[1]; back[:, :, 2] = rgb[0]   # BGR
        out = (img.astype(np.float32) * a + back * (1 - a)).astype(np.uint8)
        ok, buf = cv2.imencode(".png", out)
    else:
        b, g, r = cv2.split(img)
        al = (alpha * 255.0).round().astype("uint8")
        ok, buf = cv2.imencode(".png", cv2.merge([b, g, r, al]))
    if not ok:
        raise ValueError("PNG 編碼失敗")
    return buf.tobytes()


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
            return self._json({"ok": True, "loaded": _model is not None,
                               "bref_loaded": (_bref[1] if _bref else None), "port": PORT})
        if self.path == "/matte/models":
            return self._json({"models": bref_list(), "default": BREF_DEFAULT})
        if self.path == "/unload":
            return self._json({"unloaded": unload()})
        return self._json({"error": "unknown endpoint"}, 404)

    def do_POST(self):
        global _last
        _last = time.time()
        if self.path not in ("/segment", "/matte"):
            return self._json({"error": "unknown endpoint"}, 404)
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 48 * 1024 * 1024:
            self.close_connection = True
            return self._json({"error": "body 太大或為空"}, 413)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception as e:
            return self._json({"error": "bad json: %s" % e}, 400)
        if self.path == "/matte":
            try:
                import base64
                durl = str(body.get("image") or "")
                raw = base64.b64decode(durl.split(",", 1)[1] if "," in durl else durl)
                name = str(body.get("model") or BREF_DEFAULT)
                isz = body.get("image_size")
                t0 = time.time()
                alpha, h, w = matte(raw, name, int(isz) if isz else None)
                png = matte_png(raw, alpha, body.get("bg"))
                import base64 as b64
                return self._json({"png": "data:image/png;base64," + b64.b64encode(png).decode(),
                                   "w": w, "h": h, "model": name,
                                   "covered": float((alpha > 0.5).mean()),
                                   "soft": int(((alpha > 0.03) & (alpha < 0.97)).sum()),
                                   "elapsed": round(time.time() - t0, 2)})
            except Exception as e:
                return self._json({"error": str(e)[:300]}, 500)

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
        if idle > IDLE_UNLOAD and (_model is not None or _bref is not None):
            unload()


if __name__ == "__main__":
    threading.Thread(target=janitor, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    sys.stderr.write("SAM2 service on 127.0.0.1:%d  models=%s%s" % (PORT, MODELS_DIR, chr(10)))
    sys.stderr.flush()
    srv.serve_forever()
