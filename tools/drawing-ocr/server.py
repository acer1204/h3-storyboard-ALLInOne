# -*- coding: utf-8 -*-
"""工程圖標註定位的網頁介面。

預設只做定位（約 40 秒），把紅框畫出來讓人調提示詞與閾值。
判讀那段要再跑幾分鐘，預設關掉，需要時才勾。

不用同步請求做完才回：開工後立刻給一個 job id，前端輪詢進度。

    .venv/Scripts/python server.py            # 然後開 http://127.0.0.1:9995
"""
import base64
import io
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cv2
import numpy as np
from read_drawing import COMFY, COMFY_IN, clean, detect_boxes, has_box_node, read_crop

PORT = int(os.environ.get("DWG_PORT", "9995"))
HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "ui.html")
WORK = os.path.join(HERE, "work")          # 上傳的原圖放這裡，前端要回頭取用

JOBS = {}
JLOCK = threading.RLock()


def jset(jid, **kw):
    with JLOCK:
        j = JOBS.get(jid)
        if j:
            j.update(kw)


def jpush(jid, row):
    with JLOCK:
        j = JOBS.get(jid)
        if j:
            j["rows"].append(row)
            j["done"] = len(j["rows"])


def run_job(jid, path, prompt, thr, scale, pad, read):
    try:
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None:
            return jset(jid, state="error", error="圖片讀不開")
        h, w = img.shape[:2]
        jset(jid, state="detect", note="SAM3 定位中…", w=w, h=h)

        stem = "dwgui_%s" % jid[:8]
        staged = os.path.join(COMFY_IN, stem + ".png")
        cv2.imwrite(staged, img)
        t0 = time.time()
        raw, dt, mode = detect_boxes(stem + ".png", prompt, thr, stem, (h, w))
        if not raw:
            return jset(jid, state="error",
                        error="SAM3 沒有找到任何東西。把閾值調低，或改提示詞再試。")
        # 只做尺寸過濾（丟掉圖框級的大框與雜訊），合併交給前端。
        # 這樣調「合併強度」不必重跡一次 GPU。
        sized = [list(b) for b in raw
                 if (b[2] - b[0]) * (b[3] - b[1]) < 0.004 * w * h
                 and (b[2] - b[0]) > 12 and (b[3] - b[1]) > 12]
        boxes = clean(raw, (h, w))          # 判讀那段還是用伺服器合併過的
        jset(jid, state="read" if read else "done",
             boxes=sized, merged=[list(b) for b in boxes], total=len(boxes),
             detect_s=round(dt, 1), raw=len(sized), mode=mode,
             note=("%d 個原始框，%.1fs" % (len(sized), dt)
                   + ("" if mode == "boxes" else "（慢路：ComfyUI 沒裝取框節點）")))

        try:
            os.remove(staged)
        except OSError:
            pass

        if not read:
            return jset(jid, elapsed=round(time.time() - t0, 1))

        for i, (x0, y0, x1, y1) in enumerate(boxes):
            with JLOCK:
                if JOBS.get(jid, {}).get("cancel"):
                    return jset(jid, state="canceled")
            c = img[max(0, y0 - pad):min(h, y1 + pad), max(0, x0 - pad):min(w, x1 + pad)]
            if c.size == 0:
                continue
            c = cv2.resize(c, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            t1 = time.time()
            try:
                txt = read_crop(c)
            except Exception as e:
                txt = "?"
                jset(jid, last_error=str(e)[:120])
            jpush(jid, {"i": i, "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                        "cx": (x0 + x1) // 2, "cy": (y0 + y1) // 2,
                        "text": txt, "s": round(time.time() - t1, 2)})
        jset(jid, state="done", elapsed=round(time.time() - t0, 1))
    except Exception as e:
        jset(jid, state="error", error=str(e)[:300], trace=traceback.format_exc()[-600:])


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json; charset=utf-8", code=200):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 64 * 1024 * 1024:
            self.close_connection = True      # 沒讀完的位元組會被當成下一個請求
            return None
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            with open(PAGE, "rb") as f:
                return self._send(f.read(), "text/html; charset=utf-8")
        m = re.match(r"^/api/job/([0-9a-f]{8,32})$", p)
        if m:
            with JLOCK:
                j = JOBS.get(m.group(1))
                if not j:
                    return self._send({"error": "no such job"}, code=404)
                return self._send(dict(j))
        m = re.match(r"^/work/([A-Za-z0-9_.-]+)$", p)
        if m:
            fp = os.path.join(WORK, m.group(1))
            if not os.path.exists(fp):
                return self._send({"error": "not found"}, code=404)
            with open(fp, "rb") as f:
                return self._send(f.read(), "image/png")
        return self._send({"error": "unknown endpoint"}, code=404)

    def do_POST(self):
        p = self.path.split("?")[0]
        if p == "/api/run":
            b = self._body()
            if not isinstance(b, dict):
                return self._send({"error": "bad body"}, code=400)
            durl = str(b.get("image") or "")
            try:
                raw = base64.b64decode(durl.split(",", 1)[1] if "," in durl else durl)
            except Exception:
                return self._send({"error": "圖片解不開"}, code=400)
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                return self._send({"error": "圖片解不開"}, code=400)
            jid = uuid.uuid4().hex[:12]
            os.makedirs(WORK, exist_ok=True)
            fp = os.path.join(WORK, jid + ".png")
            cv2.imwrite(fp, img)
            with JLOCK:
                JOBS[jid] = {"id": jid, "state": "queued", "rows": [], "boxes": [],
                             "total": 0, "done": 0, "name": str(b.get("name") or ""),
                             "src": "/work/" + jid + ".png",
                             "w": img.shape[1], "h": img.shape[0], "note": ""}
            threading.Thread(target=run_job, daemon=True, kwargs=dict(
                jid=jid, path=fp,
                prompt=str(b.get("prompt") or "number:80, text:40, symbol:30"),
                thr=float(b.get("thr") or 0.10),
                scale=int(b.get("scale") or 4),
                pad=int(b.get("pad") or 6),
                read=bool(b.get("read", False)))).start()
            return self._send({"id": jid})
        m = re.match(r"^/api/job/([0-9a-f]{8,32})/cancel$", p)
        if m:
            jset(m.group(1), cancel=True)
            return self._send({"ok": True})
        return self._send({"error": "unknown endpoint"}, code=404)


def preflight():
    """先把設定錯誤講清楚。預設路徑是作者機器上的，別人 clone 下來一定要改；
    不檢的話會到真的送出一張圖才失敗，而且錯誤訊息看不出是路徑問題。"""
    bad = []
    if not os.path.isdir(COMFY_IN):
        bad.append("COMFY_INPUT 指到不存在的目錄：%s" % COMFY_IN)
    try:
        urllib.request.urlopen(COMFY + "/object_info/SAM3_Detect", timeout=5).read(1)
    except Exception as e:
        bad.append("連不上 ComfyUI 或找不到 SAM3_Detect：%s（%s）"
                   % (COMFY, str(e)[:60]))
    for line in bad:
        sys.stderr.write("  ! " + line + chr(10))
    if bad:
        sys.stderr.write("  用環境變數 COMFY_INPUT / COMFY_URL 指到你自己的位置。"
                         "伺服器還是會啟動。" + chr(10))


if __name__ == "__main__":
    # 中文訊息碰上 cp950 / cp1252 主控台會直接拋 UnicodeEncodeError，
    # 把啟動訊息弄成異常很不候。改成编不出來就替換。
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    os.makedirs(WORK, exist_ok=True)
    preflight()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    sys.stderr.write("工程圖標註擷取  http://127.0.0.1:%d%s" % (PORT, chr(10)))
    sys.stderr.flush()
    srv.serve_forever()
