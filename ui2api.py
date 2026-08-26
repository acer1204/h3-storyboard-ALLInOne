# -*- coding: utf-8 -*-
"""ComfyUI「Save」UI 格式工作流 → 可執行 API 節點圖 轉換器。

支援：子圖（definitions.subgraphs）攤平、bypass（mode 4）穿透、mute（mode 2）斷線、
Reroute 穿透、widget 值依 /object_info 的輸入順序對應（含 control_after_generate 額外欄）。
不需要先在 ComfyUI 跑過一次 —— 直接吃 Save 存出來的 .json。
"""

# UI 專用、後端不存在的節點：不輸出，僅作結構處理
UI_ONLY = {"Note", "MarkdownNote", "Reroute", "PrimitiveNode",
           "Label (rgthree)", "Fast Groups Bypasser (rgthree)", "Fast Groups Muter (rgthree)"}
# widget 型輸入（其餘視為連線型）
WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO", "IMAGEUPLOAD",
                "NUMBER", "TEXT", "SEED"}


def _norm_links(links):
    """links 兩種格式：陣列 [id,src,sslot,dst,dslot,type] 或 dict。回傳 dict: link_id -> (src,sslot,dst,dslot)"""
    out = {}
    for L in links or []:
        if isinstance(L, dict):
            out[L["id"]] = (L.get("origin_id"), L.get("origin_slot"),
                            L.get("target_id"), L.get("target_slot"))
        elif isinstance(L, (list, tuple)) and len(L) >= 5:
            out[L[0]] = (L[1], L[2], L[3], L[4])
    return out


def _widget_inputs_of(class_type, object_info):
    """依 object_info 的 required+optional 順序，列出會吃 widgets_values 的輸入。
    回傳 [(name, extra_slots)]；extra_slots=1 表示後面跟一個 control_after_generate 欄。"""
    node = object_info.get(class_type)
    if not node:
        return None
    out = []
    inp = node.get("input", {}) or {}
    for grp in ("required", "optional"):
        for name, spec in (inp.get(grp, {}) or {}).items():
            if not isinstance(spec, (list, tuple)) or not spec:
                continue
            t, opts = spec[0], (spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {})
            if opts.get("forceInput"):
                continue
            is_widget = isinstance(t, list) or (isinstance(t, str) and t.upper() in WIDGET_TYPES)
            if not is_widget:
                continue
            extra = 1 if opts.get("control_after_generate") else 0
            out.append((name, extra))
    return out


def _assign_widgets(class_type, widgets_values, object_info, warn):
    """widgets_values（list）→ {input_name: value}"""
    if not isinstance(widgets_values, list):
        return {}
    spec = _widget_inputs_of(class_type, object_info)
    if spec is None:
        warn.append("object_info 沒有節點類別 %s" % class_type)
        return {}
    vals, i, out = widgets_values, 0, {}
    for name, extra in spec:
        if i >= len(vals):
            break
        out[name] = vals[i]
        i += 1 + extra          # 跳過 control_after_generate 的 'fixed/randomize' 欄
    if i < len(vals):
        # 多出來的值多半是前端附加（預覽物件等），記錄但不當錯誤
        warn.append("%s 有 %d 個未對應的 widget 值（已忽略）" % (class_type, len(vals) - i))
    return out


class _Flattener:
    def __init__(self, ui, object_info):
        self.ui = ui
        self.oi = object_info
        self.defs = {}
        d = ui.get("definitions") or {}
        for sg in (d.get("subgraphs") or []):
            self.defs[str(sg.get("id"))] = sg
        self.api = {}          # api node id -> {class_type, inputs}
        self.warn = []
        self.errors = []

    # ---------- 主流程 ----------
    def run(self):
        self._flatten(self.ui.get("nodes") or [], self.ui.get("links") or [], prefix="", ext_in={})
        # 解析所有暫存的 link 參照 → ["node_id", slot]
        for nid, node in self.api.items():
            for k, v in list(node["inputs"].items()):
                if isinstance(v, _Ref):
                    r = self._resolve(v)
                    if r is None:
                        del node["inputs"][k]     # mute 掉的來源：斷線
                    else:
                        node["inputs"][k] = [r[0], r[1]]
        return self.api, self.warn, self.errors

    # ---------- 圖攤平（可遞迴進子圖） ----------
    def _flatten(self, nodes, links, prefix, ext_in):
        """ext_in: def-input slot index -> (值 或 _Ref)，io 節點 -10 的輸出來源"""
        lk = _norm_links(links)
        by_id = {n["id"]: n for n in nodes}
        # link_id -> 供 target 端查來源；同時記每個節點的輸出去向（bypass 用不到去向，略）
        ctx = {"lk": lk, "by_id": by_id, "prefix": prefix, "ext_in": ext_in}
        for n in nodes:
            t = str(n.get("type"))
            if t in UI_ONLY or t.startswith("workflow>"):
                continue
            if t in self.defs:
                self._flatten_instance(n, ctx)
                continue
            if t not in self.oi:
                # 不是後端節點（例如其他 UI 專用類別）→ 跳過但記警告
                self.warn.append("跳過未知節點 %s（id %s%s）" % (t, prefix, n["id"]))
                continue
            mode = n.get("mode", 0)
            if mode in (2, 4):
                continue        # mute/bypass 不輸出節點本體；連線經 _resolve 處理
            api_id = prefix + str(n["id"])
            ins = _assign_widgets(t, n.get("widgets_values"), self.oi, self.warn)
            for slot_i, inp in enumerate(n.get("inputs") or []):
                if inp.get("link") is None:
                    continue
                src = lk.get(inp["link"])
                if src is None:
                    continue
                ins[inp.get("name")] = _Ref(ctx, src[0], src[1])
            self.api[api_id] = {"class_type": t, "inputs": ins}

    def _flatten_instance(self, inst, outer_ctx):
        """子圖實例：把 def 的內部節點攤進 api，io 對應到外層。"""
        sg = self.defs[str(inst.get("type"))]
        prefix = outer_ctx["prefix"] + str(inst["id"]) + ":"
        # 1) def input slot -> 外層來源或 widget 字面值
        ext_in = {}
        by_name = {}
        for e in (inst.get("inputs") or []):
            by_name[e.get("name")] = e
        wvals = list(inst.get("widgets_values") or [])
        wi = 0
        for k, dinp in enumerate(sg.get("inputs") or []):
            name, typ = dinp.get("name"), str(dinp.get("type", ""))
            is_widget = typ.upper() in WIDGET_TYPES
            e = by_name.get(name)
            if e and e.get("link") is not None:
                src = outer_ctx["lk"].get(e["link"])
                if src is not None:
                    ext_in[k] = _Ref(outer_ctx, src[0], src[1])
                if is_widget:
                    wi += 1     # 已連線的 widget 輸入仍佔一個值欄
                continue
            if is_widget and wi < len(wvals):
                ext_in[k] = wvals[wi]
                wi += 1
        # 2) 攤平內部
        self._flatten(sg.get("nodes") or [], sg.get("links") or [], prefix, ext_in)
        # 3) 記錄實例輸出對應：inst 輸出 slot k ← 內部連到 -20 slot k 的來源
        out_map = {}
        inner_lk = _norm_links(sg.get("links") or [])
        inner_by_id = {n["id"]: n for n in (sg.get("nodes") or [])}
        inner_ctx = {"lk": inner_lk, "by_id": inner_by_id, "prefix": prefix, "ext_in": ext_in}
        for L in inner_lk.values():
            if L[2] == -20:
                out_map[L[3]] = _Ref(inner_ctx, L[0], L[1])
        self._inst_out = getattr(self, "_inst_out", {})
        self._inst_out[(outer_ctx["prefix"], inst["id"])] = out_map

    # ---------- 來源解析（穿透 bypass / Reroute / 子圖邊界 / io 節點） ----------
    def _resolve(self, ref, depth=0):
        if depth > 64:
            self.errors.append("連線解析迴圈過深")
            return None
        ctx, src_id, src_slot = ref.ctx, ref.src, ref.slot
        # 子圖輸入 io 節點：外層值
        if src_id == -10:
            v = ctx["ext_in"].get(src_slot)
            if isinstance(v, _Ref):
                return self._resolve(v, depth + 1)
            if v is None:
                return None
            return ("__LITERAL__", v)
        node = ctx["by_id"].get(src_id)
        if node is None:
            self.errors.append("找不到來源節點 %s%s" % (ctx["prefix"], src_id))
            return None
        t = str(node.get("type"))
        # 子圖實例輸出 → 內部來源
        if t in self.defs:
            om = getattr(self, "_inst_out", {}).get((ctx["prefix"], src_id), {})
            r = om.get(src_slot)
            return self._resolve(r, depth + 1) if r is not None else None
        mode = node.get("mode", 0)
        if t == "Reroute" or mode == 4:
            # 穿透：找同型別（或第一個）有連線的輸入
            want = None
            outs = node.get("outputs") or []
            if src_slot < len(outs):
                want = outs[src_slot].get("type")
            cand = None
            for inp in (node.get("inputs") or []):
                if inp.get("link") is None:
                    continue
                if cand is None:
                    cand = inp
                if want and inp.get("type") == want:
                    cand = inp
                    break
            if cand is None:
                return None
            src = ctx["lk"].get(cand["link"])
            if src is None:
                return None
            return self._resolve(_Ref(ctx, src[0], src[1]), depth + 1)
        if mode == 2:
            return None          # mute：當作沒接
        return (ctx["prefix"] + str(src_id), src_slot)


class _Ref:
    __slots__ = ("ctx", "src", "slot")
    def __init__(self, ctx, src, slot):
        self.ctx, self.src, self.slot = ctx, src, slot


def ui_to_api(ui_json, object_info):
    """UI（Save）格式 → API 節點圖。回傳 (graph, warnings)；解析失敗丟 ValueError。"""
    if not isinstance(ui_json, dict) or not isinstance(ui_json.get("nodes"), list):
        raise ValueError("不是 UI（Save）格式的工作流")
    fl = _Flattener(ui_json, object_info)
    api, warn, errors = fl.run()
    # 字面值來源（子圖輸入直接給值）攤回輸入
    for node in api.values():
        for k, v in list(node["inputs"].items()):
            if isinstance(v, list) and len(v) == 2 and v[0] == "__LITERAL__":
                node["inputs"][k] = v[1]
    if errors:
        raise ValueError("UI→API 轉換失敗：" + "；".join(errors[:5]))
    if not api:
        raise ValueError("轉換結果是空圖")
    # 斷線檢查：每個 link 參照的目標必須存在
    for nid, node in api.items():
        for k, v in node["inputs"].items():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[1], int) and str(v[0]) not in api:
                raise ValueError("節點 %s 的輸入 %s 指向不存在的 %s" % (nid, k, v[0]))
    return api, warn
