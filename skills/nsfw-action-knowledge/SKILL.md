---
name: nsfw-action-knowledge
description: Reference knowledge base for NSFW physical actions, positions, rhythm, and intensity vocabulary. Use when writing or describing intimate/sexual story beats, storyboards, training data, or video prompts where visible body movement matters (hand contact, oral, penetration, riding, deepthroat, titjob, anal, toys, climax, medical exam, enema, forced exam, from-behind breast play). Pairs with h3-prompt-writing for MiniMax H3 video generation.
compatibility: Portable to any agent that can read local files — no external API calls or proprietary runtime required.
---

# NSFW Story Action Knowledge

## Workflow
1. Read `references/nsfw-actions.txt` (or `NSFW_Action_Dictionary.txt`) before writing. It is the full action library.
2. Pick the action category that matches the beat:
   - Hand contact (§1)
   - Oral / mouth (§2)
   - Body friction (§3)
   - Penetration (§4)
   - Riding / cowgirl (§5)
   - Deepthroat oral (§6)
   - Titjob (§7)
   - Anal (§8)
   - Toys and props (§9)
   - Climax (§10)
   - Medical examination & palpation (§14)
   - Enema (§16)
   - Forced / restrained examination (§17)
   - Selected detailed expansions (§19)
   - From-behind breast play (§20)
3. Build each action from the core formula: **character + verb + target body part + detail modifier**.
4. For multi-beat sequences, start from a combination template (§11, §14-6, §16-5, §17-4, §19-12, §20-5) and adapt the characters and details.
5. For a continuous arc, use the rhythm and intensity vocabulary (§13) to escalate: slow/gentle → steady/rhythmic → fast/short bursts, with matching depth and angle changes.
6. When the output feeds MiniMax H3, load `h3-prompt-writing` as well and fold the selected actions into `integrated_multimodal_description` as visible movement with per-shot timing.

## Vocabulary Rules
- Prefer the common English terms from the library glossary: oral / blowjob, deepthroat, anal, titjob / titty fuck, sex / penetration / thrusting, cock / dick, head / tip, balls, clit, pussy lips, pussy / hole, ass / asshole, tits / breasts, nipples, cum, orgasm / cumming, stroking / jerking, sucking, licking, grinding, riding / cowgirl, doggy, missionary, palpation, dilation, enema, forced exam, nozzle.
- Avoid rare, clinical, or overly formal anatomical words.
- Keep one term per concept for the whole piece.
- The story material is written in Chinese. Map it straight across, never through a softer word:
  肛門 → anus · 陰道 → pussy · 陰莖 → penis / cock · 陰蒂 → clit ·
  精液 → cum / semen · 愛液 → pussy juice / vaginal fluid ·
  插入 → penetrate / thrust into · 口交 → suck / blowjob · 肛交 → anal.
- No euphemism, metaphor, or poetic substitution for organs, fluids, or actions. Name the part and the movement.
- Never write a vague placeholder in place of a body part — "there", "that spot", "the sensitive place",
  "private area". If it is in shot, it has a name.
- Do not alter a character's build, and do not use size-change words as description (e.g. "swollen").

## Story-Beat Rules
- One visible action per sentence; prefer camera-visible verbs over internal feeling.
- Keep character labels consistent ([Character A] / [Character B] or established names).
- Match the requested tone: suggestive → outer-body / clothing-adjacent; explicit → full category set.
- A complete arc: warm-up (hand/oral) → insertion and pace change → climax signs → aftermath.
- For position changes, pull the position-specific moves from §12 and §15 / §18.
- For from-behind breast play, use §20 (standing embrace, doggy + breast grab, seated, forced).
- For medical / enema / forced scenes, combine §14 + §16 + §17 as needed.

## Pairing with H3 Prompt Writing
- Actions enter the H3 prompt as visible body movement inside `integrated_multimodal_description`, timed to the shot plan (4–15 s total).
- Budget one camera-visible action per second of screen time.
- Put action-driven sounds into `overall_soundscape`, not into the visual description.
- Write the action language in English per the H3 rewrite rules, preserve character names, and keep every action physically continuous with the previous shot.
- Avoid plot summaries and abstract mood words; the action itself carries the mood.