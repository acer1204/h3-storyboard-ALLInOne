---
name: i2va-continue
description: Extra rules for I2VA Continue, where one image is grown into a long film by chaining clips, each starting from the last frame of the previous one. references/planner.md plans the chapters; the sections below are added to the writer's system prompt according to where the chapter sits in the chain.
---

# I2VA Continue

The web UI assembles the writer's rules from the sections below. {CHAPTER}, {TOTAL} and {SECONDS} are filled in for each chapter.

## Every chapter
This clip is one chapter of a longer film built by chaining clips: the last frame of each clip becomes the first frame of the next, and the video model remembers nothing except that frame.
- Exactly one shot: [Shot 1] only. No cuts, no [Shot 2], no scene change, no time jump.
- Spend all {SECONDS} seconds. Write the chapter's beats in order, spread evenly across the clip, and mark the pacing with "right away", "then", "as", "by the midpoint", "toward the end". The last beat must still be happening at the end of the clip.
- Every sentence must contain a visible change. Do not write the subject holding a pose, remaining, staying, waiting, keeping still, or simply looking or smiling at something for longer than a single breath. "She is still holding the cup" is not an action. If the planned beats run out early, add small physically motivated actions that fit the moment.
- Include the chapter's planned dialogue inside <d>, in the language it is written in. Keep the speaker moving while talking.
- Use the style line from the fixed settings as the opening style label of the shot, word for word, instead of judging the style from the picture. Reuse the voice description, the base soundscape and the music line word for word too, so the chapters look and sound like one film.
- Keep camera movement to small amplitude and do not end on a close-up of a body part or an object.

## First chapter
- <Picture 1> is the opening frame of the film.

## Later chapters
- <Picture 1> is the real last frame of the previous chapter, so it shows a movement caught halfway. Start from exactly what it shows: same pose, hands, props, position, framing and light. Open with one short anchor sentence for style and position, as the guide requires, then do not re-describe the scene or restart the action. The first beat finishes that movement, then the chapter moves on.

## Not the final chapter
- Do not resolve the action. The last sentence must read "The shot ends as <subject> is <doing a movement>", with a movement in progress such as turning, stepping, reaching or rising. Holding, standing, looking and smiling are not movements.
- The last frame is all the next chapter gets to see. At that moment the face must be clearly visible (frontal or three-quarter, not covered by a hand or an object) with the head and upper body in frame. Never end turned away from the camera or out of frame. If the story needs the subject to turn away or walk off, do that earlier and have them look back toward the camera before the clip ends.

## Final chapter
- This is the last chapter of the film: bring the action to a natural conclusion, and keep the subject moving until the last second. Do not end on a frozen pose or say that a pose is held until the end.
