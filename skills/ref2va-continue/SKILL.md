---
name: ref2va-continue
description: Extra rules for REF2VA Continue, where a long film is chained from clips that each continue the ending of the previous clip while a fixed reference picture holds the character's appearance. Chapter 1 is written in the base format; later chapters use the full-reference format. The sections below are added to the writer's system prompt according to where the chapter sits in the chain.
---

# REF2VA Continue

The web UI assembles the writer's rules from the sections below. {CHAPTER}, {TOTAL}, {SECONDS} and {VOICE} are filled in for each chapter.

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

## First chapter (reference)
This opening chapter is written in the full-reference format, because the film uses references from the start. There is no earlier video. Use exactly these six sections in this order:

subject_definitions:
<Subject 1> is the [person] whose face and hairstyle come from <Picture 2>, with [face and hair details], wearing [the clothing and accessories shown in <Picture 1>].
<Picture 1> is the first frame of [Shot 1], showing [what the opening frame shows].
<Subject 2> is the setting in <Picture 1>: [the background elements], with the same camera position, framing, and light.

summary:
[keyframe completion + reference generation] The target video begins from <Picture 1>, in which <Subject 1>, whose appearance follows <Picture 2>, [what happens in this chapter].

retention_analysis:
<Picture 1> ([Shot 1] first frame): fully_preserved - the pose, framing, lighting, and background of the opening frame are kept exactly.
<Subject 1> (appears in [Shot 1]): partially_preserved - the face and hairstyle match <Picture 2>; the clothing and accessories start as shown in <Picture 1> and change only as this chapter describes.
<Subject 2> (appears in [Shot 1]): fully_preserved - the background stays exactly as in <Picture 1>, without shifting, changing pattern, or gaining new elements.

detailed_description:
[The style line, one or two sentences.]
[Shot 1] The shot begins from <Picture 1>. [The chapter, in playback order.]

overall_soundscape:
[The base soundscape, plus this chapter's physical sounds.]

non_diegetic_music:
[The music line.]

- <Picture 1> and <Picture 2> may be the same picture: it is both where the film starts and what the character looks like. Take the face and hairstyle from <Picture 2>; the clothing starts as <Picture 1> shows it and changes only as the story says.
- No <Video N> label: there is no earlier clip.

## Later chapters
This chapter is written in the full-reference format, with exactly these six sections in this order:

subject_definitions:
<Subject 1> is the [person] whose face and hairstyle come from <Picture 2>, with [face and hair details], currently wearing [the clothing and accessories exactly as they are in <Picture 1>].
<Picture 1> is the first frame of [Shot 1]: the final frame of <Video 1>, showing [what that frame shows].
<Subject 2> is the setting in <Picture 1>: [the background elements], with the same camera position, framing, and light.
<Video 1> is the source video whose ending is the continuation starting point of the target video.

summary:
[video continuation + keyframe completion + reference generation] The target video continues directly from the end of <Video 1> in one continuous shot that begins from <Picture 1>, in which <Subject 1>, whose appearance follows <Picture 2>, [what happens in this chapter].

retention_analysis:
<Picture 1> ([Shot 1] first frame): fully_preserved - the pose, framing, lighting, and background of the opening frame are kept exactly.
<Video 1> (continuation starting point): fully_preserved - the motion at the end of <Video 1> carries on into the target video.
<Subject 1> (appears in [Shot 1]): partially_preserved - the face and hairstyle match <Picture 2>; the clothing and accessories follow their current state in <Picture 1>, not <Picture 2>.
<Subject 2> (appears in [Shot 1]): fully_preserved - the background stays exactly as in <Picture 1>, without shifting, changing pattern, or gaining new elements.

detailed_description:
[The style line, one or two sentences.]
[Shot 1] The shot begins from <Picture 1>, the final moment of <Video 1>. [The chapter, in playback order.]

overall_soundscape:
[The base soundscape, plus this chapter's physical sounds.]

non_diegetic_music:
[The music line.]

- Two images are attached, in order. <Picture 1> is the real last frame of the previous chapter, and it is where this shot starts: same pose, hands, props, position, framing, and light. It ends on a movement caught halfway, so the first beat finishes that movement, then the chapter moves on.
- <Picture 2> supplies the face and hairstyle only. Never begin from it, and never bring back its pose, composition, props, setting, or clothing.
- Clothing and accessories follow the story, not <Picture 2>. Anything taken off, put on, or changed in earlier chapters stays that way, exactly as <Picture 1> shows it. Describe what the character is wearing now, even when the fixed identity line or <Picture 2> shows a different outfit.
- Use no other picture or video labels: no <Picture 3>, no <Video 2>.

## Voice reference
The user attached a voice recording as <Audio 1>. What it is: {VOICE}
- Define it in subject_definitions as "<Audio 1> is the voice-timbre reference for <Subject 1> (S1)." Use the speaker ID that follows from who speaks first in this clip.
- Add "audio reference" to the task types in the summary, for example [video continuation + keyframe completion + reference generation + audio reference], or [keyframe completion + reference generation + audio reference] in the opening chapter.
- Add a retention line: "<Audio 1>: reference - <Subject 1>'s voice follows the timbre and delivery of <Audio 1> without copying the recording."
- When <Subject 1> speaks, say that the voice follows <Audio 1>. Do not use <Audio 2> or any other audio label.

## Not the final chapter
- Do not resolve the action. The last sentence must read "The shot ends as <subject> is <doing a movement>", with a movement in progress such as turning, stepping, reaching or rising. Holding, standing, looking and smiling are not movements.
- Prefer to end with the face clearly visible, frontal or three-quarter, and the head and upper body in frame. The next chapter can then be locked to this exact frame, and the background will not change at the join. End turned away only when the story requires it.

## Final chapter
- This is the last chapter of the film: bring the action to a natural conclusion, and keep the subject moving until the last second. Do not end on a frozen pose or say that a pose is held until the end.
