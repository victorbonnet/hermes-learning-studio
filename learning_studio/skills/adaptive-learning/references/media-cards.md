# Media cards

Audio, image, or video carries the question. The learner perceives something,
then responds.

## When to use

- The objective is **perceptual**: hearing a distinction, reading a signal,
  recognising a form. No amount of text substitutes for hearing the difference
  between two vowels.
- The material is inherently visual or auditory — a waveform, a specimen, a
  stack trace on screen, a piece of music.
- Authenticity matters. Real speech at real speed is a different task from a
  clean recording, and the objective may need the real thing.
- Transcription or dictation is the objective: media in, text out.

## When not to use

- The media is **decoration.** A stock photo beside a question adds load and
  teaches nothing.
- Text would do the job. If the image is a picture of a sentence, use the
  sentence — it resizes, reflows, and is readable aloud.
- The learner told you audio is unavailable or unusable and the objective does
  not require it.
- Bandwidth or device constraints make it unreliable, and the fallback is
  worse than a text item.
- The answer is a location on the image — that is
  [diagrams-and-hotspots](diagrams-and-hotspots.md).

## Required fields

- `media` — the asset reference, its type, and its duration or dimensions.
- `prompt` — what to do with the media, stated *before* it plays.
- `payload` and `answer` — from whichever response card you are combining with.
- `accessibility.alt` for images; `accessibility.transcript` for audio;
  `accessibility.captions` for video.
- `replays` — unlimited by default.
- `evaluation` — inherited from the response card.

## Evaluation

Evaluation belongs to the response, not the media: a selection response scores
as a [selection card](selection-cards.md), a typed response as a
[text-input card](text-input-cards.md).

Separate perception failures from knowledge failures. A learner who mis-hears a
word and then answers consistently with what they heard has a perception gap,
not a vocabulary gap — and the fix is more listening, not more vocabulary. Ask
what they heard when the pattern is unclear.

Count replays as data. Correct on the first pass is a different result from
correct on the fifth.

## Accessibility

This is the card type where accessibility is decided, not checked afterwards:

- **Audio needs a transcript.** The only exception is an item whose objective
  *is* listening — and then offer the transcript after the attempt.
- **Video needs captions**, and needs them for the audio content, not a
  paraphrase.
- **Images need alt text that does not give the answer away.** Describe what is
  shown, not what it means.
- Unlimited replays, learner-controlled playback, and no autoplay.
- Never make colour the discriminating feature.
- Provide a non-media path whenever the objective survives it, and say what
  changed if you substitute. See [accessibility](accessibility.md).

## Images you generate

Generate images with the host agent's existing image tool, then import the
**real** returned asset. Never invent an asset ID, filename, or path, and never
say an image was generated or imported without a real tool result. If
generation fails, describe the image in text and continue — a described diagram
is a working exercise; a fabricated reference is a broken one.

## Anti-patterns

- **Decorative media** that adds nothing but load.
- **Text as image**, which cannot be resized, searched, or read aloud.
- **Alt text that answers the question.**
- **Single-play audio**, which tests attention rather than comprehension.
- **Studio-clean audio only**, when the objective is understanding real speech.
- **Autoplay**, especially in a shared or public setting.
- **Media that needs the answer to be interpreted**, e.g. an unlabelled diagram
  the learner cannot orient themselves in.

## Combinations

- Audio stem plus [selection](selection-cards.md) for minimal-pair
  discrimination.
- Audio stem plus [text input](text-input-cards.md) for dictation.
- Image stem plus [ordering](ordering-and-matching.md) for sequencing
  photographed stages.
- Audio-front [flashcards](flashcards-and-recall.md) for listening vocabulary.
- Media as the opening evidence of a
  [scenario](scenarios-and-simulations.md).

## Examples

**Language learning.** Minimal-pair discrimination between two vowels the
learner conflates; dictation of a spoken Spanish sentence with accents
significant; a kanji image with the reading typed in; connected speech at
natural speed once clean recordings are reliably understood.

**Science.** A micrograph with the stage of mitosis selected from four options;
a titration video paused at the endpoint with "what happens if one more drop is
added?"; an audio-free animation of enzyme binding with the mechanism described
in text.

**History.** A primary-source photograph with "what does this tell you about
who commissioned it?"; a speech excerpt with the rhetorical device named; a
period map with the territorial change described after the treaty is identified.

**Programming.** A screen recording of a failing test run, with the learner
naming the first thing to check — authenticity is the point, and a transcript
of the terminal output is the accessible equivalent.
