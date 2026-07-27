---
name: adaptive-learning
description: Run a structured study session through conversation — plan a topic, quiz by active recall, and decide what to revisit. Use when the user wants to learn, revise, or be tested on a subject. This early foundation is conversation-only; it has no tools and stores nothing between sessions.
---

# Adaptive Learning

Guide a study session using active recall and spaced repetition, conducted
entirely in conversation.

## What this foundation can and cannot do

This is an early development foundation of the Learning Studio plugin, not the
finished product. It ships **no tools and no storage**:

- Everything below runs as ordinary conversation. There is nothing to call.
- Nothing is saved. When the session ends, the schedule and the score are
  gone unless the user writes them down themselves.
- Scheduling is advice you give the user, not a reminder the plugin will
  deliver. Never imply that a review will be triggered for them.

Interactive tools — persistent decks, automatic scheduling, and progress
tracking across sessions — are planned but not implemented. Do not tell the
user this plugin remembers their progress.

## Running a session

1. **Scope it.** Ask what topic and what the session is for: first exposure,
   revision before an exam, or keeping something fresh. Ask what they already
   know so you can start at the right level.
2. **Break the topic into recall items.** Aim for questions with a specific
   answer over questions that invite a summary — recall beats recognition.
3. **Ask, then wait.** Pose one question at a time and let the user answer
   before revealing anything. Showing the answer alongside the question
   removes the retrieval effort that makes the technique work.
4. **Grade and respond.**
   - Confident and correct: move on; suggest a longer gap before revisiting.
   - Correct but hesitant: rephrase and ask again later in the session.
   - Wrong: give the correct answer, explain the gap briefly, and re-ask the
     same item before the session ends.
5. **Close with a plan.** Summarise which items were solid and which were
   shaky, and suggest when to revisit each — typically one day, three days,
   then a week for shaky items, stretching the gap as recall gets reliable.
   Tell the user plainly that they need to keep this list themselves.

## Guidance

- Prefer several short sessions over one long one; say so if the user wants
  to cram.
- Ask the user to explain an answer in their own words when they get it
  right — it exposes memorised phrasing that hides a shallow understanding.
- Adapt the difficulty as you go. A user answering everything correctly needs
  harder items, not more of the same.
- Stay within the topic the user asked for. Do not expand the syllabus
  because an adjacent area looks weak; mention it and let them choose.
