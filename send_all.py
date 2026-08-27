# -*- coding: utf-8 -*-
"""依序把四模式的歷史紀錄送 ComfyUI 生成，含 GPU 釋放等待與狀態輪詢。"""
import json, sys, time, urllib.request

BASE = "http://localhost:9998"

def api(path, body=None, timeout=120):
    req = urllib.request.Request(BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read() or b"{}")

def wait_gpu_free(tag, limit_s=300):
    t0 = time.time()
    while time.time() - t0 < limit_s:
        g = api("/api/gpu")
        used = g.get("used_mb")
        if used is None or used <= 4000:
            print(f"[{tag}] GPU ready ({used} MB)", flush=True)
            return True
        print(f"[{tag}] waiting llama unload... VRAM {used} MB", flush=True)
        time.sleep(10)
    print(f"[{tag}] GPU wait timeout, proceeding anyway", flush=True)
    return False

def run_one(rid, tag, first=False):
    # 只有第一筆要等 GPU（防 llama 佔用）；連續送單時高 VRAM 是 ComfyUI 自己的模型駐留，直接送
    import os
    if first and not os.environ.get("H3_SKIP_WAIT"):
        wait_gpu_free(tag)
    try:
        r = api("/api/comfy/run", {"rec_id": rid, "dur": "20 seconds"}, timeout=300)
    except Exception as e:
        print(f"[{tag}] SUBMIT FAILED: {e}", flush=True)
        return False
    if "error" in r:
        print(f"[{tag}] REJECTED: {r['error'][:300]}", flush=True)
        return False
    pid = r["prompt_id"]
    print(f"[{tag}] queued prompt_id={pid}", flush=True)
    t0 = time.time()
    last = ""
    while time.time() - t0 < 30 * 60:
        time.sleep(12)
        try:
            s = api("/api/comfy/status/" + pid, timeout=30)
        except Exception as e:
            print(f"[{tag}] status err {e}", flush=True)
            continue
        st = s.get("state", "?")
        if st != last:
            print(f"[{tag}] {st} {json.dumps(s.get('outputs') or '', ensure_ascii=False)[:120]}", flush=True)
            last = st
        if st in ("done", "error"):
            return st == "done"
    print(f"[{tag}] TIMEOUT after 30min", flush=True)
    return False

if __name__ == "__main__":
    jobs = json.loads(sys.argv[1])
    results = {}
    for j, (tag, rid) in enumerate(jobs):
        print(f"===== {tag} ({rid}) =====", flush=True)
        results[tag] = run_one(rid, tag, first=(j == 0))
        time.sleep(5)
    print("SUMMARY:", json.dumps(results), flush=True)
