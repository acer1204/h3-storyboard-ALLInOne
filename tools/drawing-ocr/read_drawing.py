# -*- coding: utf-8 -*-
"""工程圖標註擷取：SAM3 找位置，視覺模型讀內容。

兩段是刻意分開的。SAM3 只會分割，給不出文字；視覺模型直接讀整張大圖時，
標註太小又埋在線條裡，容易漏。先框再逐框放大讀，每個裁切都是乾淨的小圖。

用法：
    python read_drawing.py <圖檔> [--out 結果.csv] [--prompt "number:80"] [--thr 0.10]

輸出 CSV 與 JSON，每列是一個標註的中心座標與讀到的文字。
"""
import argparse
import base64
import csv
import glob
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.request

import cv2
import numpy as np

COMFY = os.environ.get("COMFY_URL", "http://127.0.0.1:9997")
LLAMA = os.environ.get("LLAMA_URL", "http://127.0.0.1:9999")
SAM3_CKPT = os.environ.get("SAM3_CKPT", "sam3.1_multiplex_fp16.safetensors")
VLM = os.environ.get("VLM_MODEL", "qwen3.8-27b")
# ComfyUI 只吃 input 目錄裡的檔，所以要先複製進去
COMFY_IN = os.environ.get("COMFY_INPUT", "E:/ComfyUI-MiniMaxH3/ComfyUI/input")
COMFY_OUT = os.environ.get("COMFY_OUTPUT", "E:/ComfyUI-MiniMaxH3/ComfyUI/output")


def _post(path, body, timeout=300):
    req = urllib.request.Request(COMFY + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())


def _get(path, timeout=300):
    return json.loads(urllib.request.urlopen(COMFY + path, timeout=timeout).read().decode())


def detect(name, prompt, thr, tag):
    """SAM3 文字定位。individual_masks 一定要開，否則只拿得到一張聯集遮罩。"""
    wf = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": SAM3_CKPT}},
        "2": {"class_type": "LoadImage", "inputs": {"image": name}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": prompt}},
        "4": {"class_type": "SAM3_Detect",
              "inputs": {"model": ["1", 0], "image": ["2", 0], "conditioning": ["3", 0],
                         "threshold": thr, "refine_iterations": 1, "individual_masks": True}},
        "5": {"class_type": "MaskToImage", "inputs": {"mask": ["4", 0]}},
        "6": {"class_type": "SaveImage", "inputs": {"images": ["5", 0], "filename_prefix": tag}},
    }
    for f in glob.glob(os.path.join(COMFY_OUT, tag + "_*.png")):
        os.remove(f)
    t0 = time.time()
    pid = _post("/prompt", {"prompt": wf})["prompt_id"]
    while time.time() - t0 < 600:
        time.sleep(2)
        h = _get("/history/" + pid)
        st = h.get(pid, {}).get("status", {})
        if st.get("completed"):
            break
        if st.get("status_str") == "error":
            raise RuntimeError("SAM3 執行失敗：" + json.dumps(st, ensure_ascii=False)[:300])
    return sorted(glob.glob(os.path.join(COMFY_OUT, tag + "_*.png"))), time.time() - t0


def masks_to_boxes(files, shape):
    h, w = shape
    out = []
    for f in files:
        m = cv2.imread(f, cv2.IMREAD_GRAYSCALE)
        if m is None:
            continue
        if m.shape[:2] != (h, w):
            m = cv2.resize(m, (w, h), interpolation=cv2.INTER_NEAREST)
        ys, xs = np.nonzero(m > 127)
        if len(xs) < 15:
            continue
        out.append((int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())))
    return out


def _inter(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    return 0 if (x1 <= x0 or y1 <= y0) else (x1 - x0) * (y1 - y0)


def _area(b):
    return (b[2] - b[0]) * (b[3] - b[1])


def _iou(a, b):
    i = _inter(a, b)
    return i / max(1, _area(a) + _area(b) - i)


def _contain(a, b):
    """交集佔「較小那個框」的比例。片段被完整框包住時接近 1，
    IoU 在這種包含關係下會很小，所以單看 IoU 會把 R1 跟 R140 當成兩個東西。"""
    i = _inter(a, b)
    return i / max(1, min(_area(a), _area(b)))


def clean(boxes, shape, max_area=0.004, thr=0.3):
    """SAM3 對同一個標註會回好幾個範圍不同的框，有時還把一個標註切成兩段
    （R140 -> R1 + 140），另外會混進圖框級的大框。

    去重不夠，要合併：兩個框只要重疊到一定程度就敲成它們的聯集，
    反覆做到不再變動。這樣重複框收成一個，被切開的標註也接回來。"""
    h, w = shape
    bs = [list(b) for b in boxes
          if _area(b) < max_area * w * h and (b[2] - b[0]) > 12 and (b[3] - b[1]) > 12]
    changed = True
    while changed:
        changed = False
        out, used = [], [False] * len(bs)
        for i, a in enumerate(bs):
            if used[i]:
                continue
            cur = a[:]
            for j in range(i + 1, len(bs)):
                if used[j]:
                    continue
                b = bs[j]
                if _contain(cur, b) >= thr or _iou(cur, b) >= thr:
                    cur = [min(cur[0], b[0]), min(cur[1], b[1]),
                           max(cur[2], b[2]), max(cur[3], b[3])]
                    used[j] = True
                    changed = True
            used[i] = True
            out.append(cur)
        bs = out
    bs.sort(key=lambda b: (b[1], b[0]))
    return [tuple(b) for b in bs]


def read_crop(crop):
    ok, buf = cv2.imencode(".png", crop)
    url = "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode()
    body = {"model": VLM, "stream": False, "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "text",
                 "text": "這是工程圖的局部裁切，裡面有一個尺寸標註。"
                         "只回答文字內容本身，不要說明、不要標點。旋轉的文字請正讀。看不出來回 ?"},
                {"type": "image_url", "image_url": {"url": url}}]}]}
    req = urllib.request.Request(LLAMA + "/v1/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    j = json.loads(urllib.request.urlopen(req, timeout=180).read().decode())
    return j["choices"][0]["message"]["content"].strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--out", default="")
    ap.add_argument("--prompt", default="number:80, text:40, symbol:30")
    ap.add_argument("--thr", type=float, default=0.10)
    ap.add_argument("--scale", type=int, default=4, help="裁切後放大倍率，小字要夠大才讀得準")
    ap.add_argument("--pad", type=int, default=6)
    ap.add_argument("--boxes-only", action="store_true", help="只定位不判讀")
    a = ap.parse_args()

    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    src = os.path.abspath(a.image)
    img = cv2.imread(src, cv2.IMREAD_COLOR)
    if img is None:
        out.write("讀不開：%s%s" % (src, chr(10)))
        return 1
    h, w = img.shape[:2]

    # 前網要每次不同：ComfyUI 會快取相同的圖，重跡時直接回上一次的結果而
    # 不重新寫檔，舊檔已經被我們刪掉的話就拿到 0 張遮罩。
    stem = "dwgocr_%d_" % (int(time.time()) % 100000) +            re.sub(r"[^A-Za-z0-9]", "", os.path.basename(src))[:12]
    staged = os.path.join(COMFY_IN, stem + ".png")
    cv2.imwrite(staged, img)

    out.write("圖 %dx%d  提示詞 %r  閾值 %.2f%s" % (w, h, a.prompt, a.thr, chr(10)))
    files, dt = detect(stem + ".png", a.prompt, a.thr, stem)
    if not files:
        out.write("SAM3 沒有回任何遮罩。降低 --thr 或改提示詞再試。%s" % chr(10))
        return 1
    raw_boxes = masks_to_boxes(files, (h, w))
    boxes = clean(raw_boxes, (h, w))
    out.write("SAM3 %.1fs：%d 張遮罩 -> %d 個原始框 -> 合併後 %d 個%s"
              % (dt, len(files), len(raw_boxes), len(boxes), chr(10)))

    vis = img.copy()
    for i, (x0, y0, x1, y1) in enumerate(boxes):
        cv2.rectangle(vis, (x0, y0), (x1, y1), (0, 200, 0), 3)
        cv2.putText(vis, str(i), (x0, max(14, y0 - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 220), 2, cv2.LINE_AA)
    base = a.out or (os.path.splitext(src)[0] + "-標註")
    cv2.imwrite(base + ".png", vis)
    out.write("框位置圖：%s.png%s" % (base, chr(10)))

    rows = []
    if not a.boxes_only:
        t0 = time.time()
        for x0, y0, x1, y1 in boxes:
            c = img[max(0, y0 - a.pad):min(h, y1 + a.pad),
                    max(0, x0 - a.pad):min(w, x1 + a.pad)]
            if c.size == 0:
                continue
            c = cv2.resize(c, (0, 0), fx=a.scale, fy=a.scale, interpolation=cv2.INTER_CUBIC)
            try:
                txt = read_crop(c)
            except Exception:
                txt = "?"
            rows.append({"x": (x0 + x1) // 2, "y": (y0 + y1) // 2,
                         "x0": x0, "y0": y0, "x1": x1, "y1": y1, "text": txt})
        out.write("判讀 %d 個，%.1fs（平均 %.2fs/框）%s"
                  % (len(rows), time.time() - t0, (time.time() - t0) / max(1, len(rows)), chr(10)))

        with io.open(base + ".csv", "w", encoding="utf-8-sig", newline="") as f:
            wcsv = csv.DictWriter(f, fieldnames=["x", "y", "x0", "y0", "x1", "y1", "text"])
            wcsv.writeheader()
            wcsv.writerows(rows)
        json.dump(rows, io.open(base + ".json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        out.write("結果：%s.csv / %s.json%s" % (base, base, chr(10)))
        for r in sorted(rows, key=lambda r: (r["y"], r["x"])):
            out.write("  (%5d,%5d)  %s%s" % (r["x"], r["y"], r["text"][:30], chr(10)))

    try:
        os.remove(staged)
        for f in files:
            os.remove(f)
    except OSError:
        pass
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
