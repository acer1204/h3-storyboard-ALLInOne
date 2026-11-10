---
name: ref2va-continue
description: Extra rules for REF2VA Continue, where a long film is chained from clips that each continue the ending of the previous clip while a fixed reference picture holds the character's appearance. Chapter 1 is written in the base format; later chapters use the full-reference format. The sections below are added to the writer's system prompt according to where the chapter sits in the chain.
---

# REF2VA Continue

The web UI assembles the writer's rules from the sections below. {CHAPTER}, {TOTAL} and {SECONDS} are filled in for each chapter.

## Every chapter
This clip is one chapter of a longer film built by chaining clips.
- Exactly one shot: [Shot 1] only. No cuts, no [Shot 2], no scene change, no time jump.
- Spend all {SECONDS} seconds. Write the chapter's beats in order, spread evenly across the clip, and mark the pacing with "right away", "then", "as", "by the midpoint", "toward the end". The last beat must still be happening at the end of the clip.
- Every sentence must contain a visible change. Do not write the subject holding a pose, remaining, staying, waiting, keeping still, or simply looking or smiling at something for longer than a single breath. If the planned beats run out early, add small physically motivated actions that fit the moment.
- Include the chapter's planned dialogue inside <d>, in the language it is written in. Keep the speaker moving while talking.
- Use the style line from the fixed settings as the style opening, word for word, instead of judging the style from the picture. Reuse the voice description, the base soundscape and the music line word for word too, so the chapters look and sound like one film.
- Keep camera movement to small amplitude and do not end on a close-up of a body part or an object.

## First chapter
- <Picture 1> is the opening frame of the film. Write this chapter in the normal three-section format.

## Later chapters
This chapter is written in the full-reference format, with exactly these six sections in this order, and these labels only:

subject_definitions:
<Subject 1> is the [person] whose appearance comes from <Picture 1>, with [the fixed identity details].
<Subject 2> is the setting in the final moment of <Video 1>: [the background elements], with the same camera position, framing, and light.
<Video 1> is the source video whose ending is the continuation starting point of the target video, showing [what its final moment shows].

summary:
[video continuation + reference generation] The target video continues directly from the end of <Video 1> in one continuous shot, in which <Subject 1>, whose appearance follows <Picture 1>, [what happens in this chapter].

retention_analysis:
<Video 1> (continuation starting point): fully_preserved - the framing, pose, lighting, and background at the end of <Video 1> carry over as the opening of the target video.
<Subject 1> (appears in [Shot 1]): fully_preserved - the face, hairstyle, jewelry, and costume match <Picture 1> throughout.
<Subject 2> (appears in [Shot 1]): fully_preserved - the background stays exactly as in the final moment of <Video 1>, without shifting, changing pattern, or gaining new elements.

detailed_description:
[The style line, one or two sentences.]
[Shot 1] The shot resumes from the final moment of <Video 1>. [The chapter, in playback order.]

overall_soundscape:
[The base soundscape, plus this chapter's physical sounds.]

non_diegetic_music:
[The music line.]

- <Picture 1> supplies appearance only. Never begin from it, and never bring back its pose, composition, props, or setting.
- <Video 1> is the only starting point. Open from exactly where it ends: same pose, hands, props, position, framing, and light. It ends on a movement caught halfway, so the first beat finishes that movement, then the chapter moves on.
- Two images are attached. The first is the real last frame of the previous chapter, for you to see where the action stands; it has no label, so never call it <Picture>. The second is <Picture 1>.
- Use no other reference labels: no <Picture 2>, no <Video 2>, no <Audio N>.

## Not the final chapter
- Do not resolve the action. The last sentence must read "The shot ends as <subject> is <doing a movement>", with a movement in progress such as turning, stepping, reaching or rising. Holding, standing, looking and smiling are not movements.
- Prefer to end with the face clearly visible, frontal or three-quarter, and the head and upper body in frame. The next chapter can then be locked to this exact frame, and the background will not change at the join. End turned away only when the story requires it.

## Final chapter
- This is the last chapter of the film: bring the action to a natural conclusion, and keep the subject moving until the last second. Do not end on a frozen pose or say that a pose is held until the end.
