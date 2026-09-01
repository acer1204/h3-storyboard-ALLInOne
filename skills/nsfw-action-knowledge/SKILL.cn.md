---
name: nsfw-action-knowledge
description: NSFW 身體動作、姿勢、節奏與力度詞彙的參考知識庫。撰寫或描述包含親密／性愛行為的故事段落、分鏡、訓練資料或影片提示詞（重點在可見身體動作：手部接觸、口交、插入、騎乘、深喉、乳交、肛交、道具、高潮）時使用。與 h3-prompt-writing 搭配可強化 MiniMax H3 影片生成。
compatibility: 可移植到任何能讀取本地檔案的 agent——無外部 API 呼叫或專有執行環境。
---

# NSFW Story Action 知識庫（中文版）

## 工作流程

1. 動筆前先讀 `references/nsfw-actions.txt`——完整動作庫：中英對照詞彙表、十大動作分類、九個組合模板、姿勢補充、節奏與力度詞彙。
2. 依拍點選擇對應動作分類：手部接觸（§1）、口部／舌頭（§2）、身體貼合摩擦（§3）、插入抽插（§4）、騎乘（§5）、深喉（§6）、乳交（§7）、肛交（§8）、道具（§9）、高潮射精（§10）。
3. 每個動作遵循核心公式：**角色 + 動詞 + 目標部位 + 細節修飾**（例：「手指彎曲向上勾動前壁」；「臀部抬起重重落下，陰部撞擊大腿」）。
4. 多拍連續動作，以組合模板（§11）為起點改角色與細節，不要從零憑空編排。
5. 連續情緒弧線用節奏與力度詞彙（§13）遞進：慢速輕柔 → 穩定有節奏 → 快速短促，並搭配深度與角度變化。
6. 若輸出要送入 MiniMax H3，同時載入 `h3-prompt-writing`，把選定動作以「可見身體動作＋分鏡時間」寫入 `integrated_multimodal_description`。

## 詞彙規則

- 使用詞彙表中的常見英文詞：oral / blowjob、deepthroat、anal、titjob / titty fuck、sex / penetration / thrusting、cock / dick、head / tip、balls、clit、pussy lips、pussy / hole、ass / asshole、tits / breasts、nipples、cum、orgasm / cumming、stroking / jerking、sucking、licking、grinding、riding / cowgirl、doggy、missionary。
- 避免罕見、臨床化或過度正式的解剖專名；以詞彙表為允許清單。
- 同一概念整篇只用一個詞——例如一次叫 "clit"，就全程用 "clit"。

## 故事段落規則

- 一句一個可見動作；優先用鏡頭看得見的字，而非內心感受（「雙腿夾緊腰，腳趾蜷曲」勝於「她感覺……」）。
- 角色標籤在拍點與整段序列中保持一致（[角色A]／[角色B] 或已建立的稱呼）。
- 貼合要求的水准：暗示級（suggestive）用外圍動作（鎖骨指尖、大腿、乳房、摩擦）；露骨級（explicit）用完整分類。
- 完整弧線：暖場（手／口）→ 插入與節奏變化 → 高潮徵兆（夾腿、弓身、顫抖、腳趾蜷曲）→ 事後（緩慢抽出、停留、輕研磨、射精）。
- 姿勢轉換時，從 §12 拿對應姿勢的動作補充（傳教士、後入、騎乘、側入、站立），保持身體物理合理。

## 與 H3 Prompt Writing 搭配

- 動作在 H3 提示詞中是 `integrated_multimodal_description` 內的可見身體動作，依分鏡計時（總長 4–15 秒）。
- 每秒螢幕時間預算一個可見動作；10 秒鏡頭帶 3–6 拍遞進序列，而非 15 個微動作。
- 動作相關聲音（呼吸、皮膚聲、水聲、喘息）寫入 `overall_soundscape`，不混進視覺描述。
- 依 H3 重寫規則用英文寫動作描述、保留使用者角色名、每個動作與前一鏡頭物理連續。
- 避免劇情概括與抽象情緒詞；情緒由動作本身呈現。
