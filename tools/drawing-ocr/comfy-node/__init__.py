# -*- coding: utf-8 -*-
"""把偵測器已經算好的框直接送回 API。

SAM3_Detect 有兩個輸出：masks 與 bboxes。只要框的話，masks 那條是純浪費——
個別遮罩會產生 [N, 高, 寬] 的浮點張量（143 個偵測、3309x2339 就是 4.4GB），
再轉成 RGB、再編碼成 N 張 PNG、再讓呼叫端讀回來用 nonzero 反推出矩形。
偵測器本來就吐 bboxes，而且已經換算回原圖座標。

ComfyUI 沒有節點能把 BoundingBox 送出來，所以補這一個。它不碰 GPU，
沒有輸出檔，框以 JSON 走 /history 回去。

裝法：把這個目錄複製到 ComfyUI 的 custom_nodes/，重開 ComfyUI。
不裝也不會壞——呼叫端找不到節點時會退回原本的遮罩路徑，只是慢。
"""
import json

from typing_extensions import override

from comfy_api.latest import ComfyExtension, io


class BoundingBoxesToJSON(io.ComfyNode):
    """框 -> JSON 字串，同時放進 /history 的 outputs 讓呼叫端直接取。"""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="BoundingBoxesToJSON",
            display_name="Bounding Boxes to JSON",
            category="image/detection",
            search_aliases=["bbox", "boxes", "json", "detection", "export"],
            inputs=[
                io.BoundingBox.Input("bboxes", display_name="bboxes", force_input=True,
                                     tooltip="偵測節點的 bboxes 輸出"),
            ],
            outputs=[io.String.Output("json")],
            # 少了這行 ComfyUI 會認定整張圖沒有終點，一個節點都不跑。
            is_output_node=True,
        )

    @classmethod
    def execute(cls, bboxes) -> io.NodeOutput:
        # 可能是單一 dict、一層 list，或每張圖一層的 list[list]。原樣送出，
        # 呼叫端自己攤平——這裡不猜格式，免得偵測器換了就對不上。
        txt = json.dumps(bboxes, ensure_ascii=False)
        return io.NodeOutput(txt, ui={"text": [txt]})


class BoundingBoxesExportExtension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [BoundingBoxesToJSON]


async def comfy_entrypoint() -> BoundingBoxesExportExtension:
    return BoundingBoxesExportExtension()
