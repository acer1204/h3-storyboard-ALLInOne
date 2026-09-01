---
name: nsfw-action-knowledge
description: Reference knowledge base for NSFW physical actions, positions, rhythm, and intensity vocabulary. Use when writing or describing intimate/sexual story beats, storyboards, training data, or video prompts where visible body movement matters (hand contact, oral, penetration, riding, deepthroat, titjob, anal, toys, climax). Pairs with h3-prompt-writing for MiniMax H3 video generation.
compatibility: Portable to any agent that can read local files — no external API calls or proprietary runtime required.
---

# NSFW Story Action Knowledge

## Workflow

1. Read `references/nsfw-actions.txt` before writing. It is the full action library: a Chinese-English glossary, ten action categories, nine combination templates, position-specific moves, and rhythm/intensity vocabulary.
2. Pick the action category that matches the beat: hand contact (§1), oral/mouth (§2), body friction (§3), penetration (§4), riding (§5), deepthroat oral (§6), titjob (§7), anal (§8), toys and props (§9), climax (§10).
3. Build each action from the core formula: **character + verb + target body part + detail modifier** (e.g. "fingers curve upward inside, pressing the front wall"; "hips lift and drop hard, crotch striking the thigh").
4. For multi-beat sequences, start from a combination template (library §11) and adapt the characters and details instead of inventing a sequence from scratch.
5. For a continuous arc, use the rhythm and intensity vocabulary (library §13) to escalate: slow/gentle → steady/rhythmic → fast/short bursts, with matching depth and angle changes.
6. When the output feeds MiniMax H3, load `h3-prompt-writing` as well and fold the selected actions into `integrated_multimodal_description` as visible movement with per-shot timing.

## Vocabulary Rules

- Use the common English terms from the library glossary: oral / blowjob, deepthroat, anal, titjob / titty fuck, sex / penetration / thrusting, cock / dick, head / tip, balls, clit, pussy lips, pussy / hole, ass / asshole, tits / breasts, nipples, cum, orgasm / cumming, stroking / jerking, sucking, licking, grinding, riding / cowgirl, doggy, missionary.
- Avoid rare, clinical, or overly formal anatomical words; the glossary is the allowed list.
- Keep one term per concept for the whole piece — if you call it "clit" once, stay with "clit".

## Story-Beat Rules

- One visible action per sentence; prefer camera-visible verbs over internal feeling ("thighs clamp around the waist, toes curl" beats "she feels...").
- Keep character labels consistent across the beat and the whole sequence ([Character A] / [Character B] or the established names).
- Match the requested tone: for suggestive beats use outer-body and clothing-adjacent actions (fingertips at the collarbone, thigh, breast, grinding); for explicit beats use the full category set.
- A complete arc: warm-up (hand/oral) → insertion and pace change → climax signs (legs clamping, arching, trembling, toes curling) → aftermath (slow withdrawal, lingering, slow grinding, cumming on skin).
- For position changes, pull the position-specific moves from library §12 (missionary, doggy, cowgirl, side, standing) so the bodies stay physically plausible.

## Pairing with H3 Prompt Writing

- Actions enter the H3 prompt as visible body movement inside `integrated_multimodal_description`, timed to the shot plan (4–15 s total).
- Budget one camera-visible action per second of screen time; a 10-second shot carries a short escalating sequence of 3–6 beats, not 15 micro-actions.
- Put action-driven sounds (breathing, skin-on-skin, wet sounds, moans) into `overall_soundscape`, not into the visual description.
- Write the action language in English per the H3 rewrite rules, preserve the user's character names, and keep every action physically continuous with the previous shot.
- Avoid plot summaries and abstract mood words; the action itself carries the mood.
