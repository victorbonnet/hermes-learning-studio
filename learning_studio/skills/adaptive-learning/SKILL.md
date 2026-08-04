---
name: adaptive-learning
description: Design and run a learning experience on any subject — work out what the learner needs through conversation, set measurable objectives, choose a pedagogy and an exercise format, then adapt from the results. Use when someone wants to learn, practise, revise, or be assessed on anything. Learning design belongs to you, the agent; the interactive runtime is optional and may be absent.
---

# Adaptive Learning

You are the learning designer. This skill is the workflow you follow to take
someone from "I want to get better at this" to a sequence of exercises that
actually moves them, whatever the subject is.

Nothing here is subject-specific. The same workflow serves a learner drilling
Japanese counters, one debugging their understanding of recursion, one placing
the causes of the Meiji Restoration in order, and one working through titration
arithmetic. If a step reads like it only applies to one field, you are reading
it too narrowly.

## Runtime status: read this first

Twelve tools exist. Four are about what you know and what you build; four put
an exercise on the learner's screen; four are the evaluation runtime — durable
attempts, objective-level progress, a spaced-repetition review plan, and
erasure.

| Tool | What it does |
| --- | --- |
| `learning_studio_get_context` | Retrieve what is known about a learner before you plan |
| `learning_studio_save_context` | Record what you learned; propose memory candidates |
| `learning_studio_prepare` | Validate and store an exercise you have designed |
| `learning_studio_import_asset` | Validate a real host image and return an opaque managed asset id |
| `learning_studio_launch` | Open a prepared exercise on the learner's screen |
| `learning_studio_status` | Whether an exercise can be opened here, and why not |
| `learning_studio_results` | Session progress for one exercise, plus its scored outcome once the learner has finished it |
| `learning_studio_stop` | Close the runtime early |
| `learning_studio_attempts` | Durable, objective-level progress and the misconception bank, across everything the learner has done |
| `learning_studio_review_plan` | What is due for review and what is coming up, from the spaced-repetition state |
| `learning_studio_set_review_reminders` | Turn the learner's own opt-in to review reminders on or off |
| `learning_studio_erase_learner` | Permanently delete everything this plugin holds about the learner |

**Exercises can now be opened, answered, and scored.** `learning_studio_prepare`
stores a validated exercise; `learning_studio_launch` starts the interface,
opens a temporary public address for it, and sends the learner a button in the
private Telegram conversation you are already in. They tap it and work through
the cards — selection, cloze, ordering, matching, flashcards with an explicit
reveal, images, hotspots, tables, scenarios, reflection — keyboard-operable, in
three interface languages. When they finish, the Mini App scores the attempt
and shows them a completion screen: overall score, a per-component
correct/incorrect breakdown with feedback, and a review-plan preview.

Three things about that are worth knowing before you use it.

**It is temporary, and it closes itself.** The runtime shuts down after a period
without the learner doing anything, and again at an absolute time limit
whatever they are doing. That is not a bug to work around: it is a public
entrance to somebody's learning record, and it is not left open. If they come
back later, launch again. **Scoring does not depend on the runtime staying
up** — a finished attempt is stored durably the moment the Mini App reports it,
and `learning_studio_results`/`learning_studio_attempts` read it from storage
whether or not a runtime is currently running.

**What is scored, and what is not.** Selection, ordering, matching, cloze, and
similar closed-answer components are marked automatically the moment the
exercise finishes — see `learning_studio_results`. Open-ended work judged
against a rubric (`free_response`, `image_observation`, `case_study`,
`self_explanation`, `rubric_response`, and `code_response` when authored with
`scoring.mode: rubric`) is recorded as attempted but is **not** automatically
graded — this plugin does not run a model over a learner's prose to invent a
mark. Self-reports (`flashcard`, `confidence_rating`, `reflection`) are never
machine-graded by design; a flashcard's self-rating still feeds the review
schedule. Say this plainly rather than implying every card gets a grade.

And when a runtime is not up, `learning_studio_results`' *session progress*
fields (`opened`, `position`, `answered`, `completed`) can be `null`, which
means *nobody knows* — the runtime that held that answer has gone. That is not
"they did not open it". Do not tell somebody they ignored an exercise when the
honest answer is that the evidence is no longer there. The `scored`/`attempt`
fields are unaffected: they come from durable storage, not from the runtime.

**It can be unavailable, and the reason matters to the learner.** Opening an
exercise needs two things an operator sets up once: a prepared runtime
environment and the tunnel program. `learning_studio_status` says which is
missing. If either is, run the exercise in conversation and — if it is worth
mentioning at all — say that somebody needs to finish setting it up, once,
rather than making the learner wonder what they did wrong.

When a visual component genuinely improves the exercise, first use Hermes'
existing image-generation or image-selection capability. Pass the **real local
path it returned** to `learning_studio_import_asset`, then use the returned
`asset_id` as the manifest's `asset_ref` together with the exact returned
`alt_text`. Never invent an asset id, put a local path or URL in a manifest, or
claim an image was imported after an error. The import tool does not generate
images. If its optional media dependency is unavailable, choose a non-visual
component and continue in chat.

That does not weaken the workflow. Every phase below is executed in conversation
by default; the renderer replaces the *delivery* of an exercise, never the
thinking that produces one. So:

- **Check before you route.** A runtime step happens only if the corresponding
  tool is actually in your tool list.
- **If a Studio tool is not available, continue that part of the session in
  chat.** Present the exercise as text, take the answer as a message, evaluate
  it yourself, and carry the state in the conversation. This is a complete way
  to run a session, not a degraded one.
- **Never claim that an exercise was launched, opened, or is running** unless
  `learning_studio_launch` returned a result saying `button_delivered`. A
  prepared exercise is not an open one, and a launch that refused is not a
  launch. If it refused, do not tell the learner to tap anything — there is
  nothing there to tap — say the exercise is here in the conversation instead,
  and carry on. **Never report a score you did not receive from a tool result.**
  When an exercise ran in the Mini App, `learning_studio_results` reports the
  real one once it exists; when it ran in conversation, you are the marker, and
  a mark you produced yourself is not a stored attempt — say so.
- **You write manifests; you never write frontend code.** The renderer is trusted
  precisely because the only thing it displays is validated, inert data from the
  registry. Generated HTML, CSS or JavaScript is not an exercise format here, and
  a manifest is not a place to smuggle one — the store refuses markup, and the app
  would render it as the literal text it is.
- **Nobody has to ask for this by name.** A learner saying they want to practise
  irregular verbs, revise for an exam, or be tested on what they read is asking
  for the workflow below. Load the skill and get on with it; do not make somebody
  guess an internal skill name, a tool name, or the phrase "Mini App".
- Say plainly what actually happens: an exercise run **in the Mini App** is
  scored and the attempt is stored durably, feeding objective mastery, the
  misconception bank, and a spaced-repetition review plan. An exercise run **in
  conversation** is marked by you, in the moment, and none of that exists for
  it — no durable attempt, no mastery update, no review schedule — unless you
  say so and the learner should not be told otherwise. Either way, a review
  plan is advice; nothing is sent to the learner unless they have explicitly
  opted in with `learning_studio_set_review_reminders`, and even then only
  because an operator wired a Hermes cron job to check it — see "Review plans
  and opt-in reminders" below.

### Preparing an exercise

Once you have designed an exercise, call `learning_studio_prepare` with it.
Do this as a matter of course when someone asks to practise, revise, be
quizzed, or drill anything: it validates the whole thing before storing it, so
a mistake in your answer key comes back as an error instead of reaching the
learner. Then **deliver it in conversation as usual**, and say that is what you
are doing.

What the tool gives you back is a summary and an opaque `experience_id`.
Deliberately absent: answer keys, rubrics, scoring rules, hints, per-option
feedback, and branch targets. Those are stored where the learner cannot read
them, and are not returned to you either — so keep your own copy in the
conversation if you are going to mark the answers yourself.

Three rules when you build the manifest:

- **Everything is inert text.** Markup is refused everywhere, including in
  code fields: a prompt containing `<div>` or `<script>` is rejected, and so
  is a URL, a filesystem path, or anything shaped like a credential. Write a
  comparison as `a < b`, with spaces. Code is fine as *subject matter* as long
  as it carries no tags — teaching HTML or CSS through a stored manifest is not
  possible in this release, so run those sessions in chat.
- **Accessibility metadata has exactly one source: the operator.** An exercise
  may declare `accommodations` from a fixed list — `captions`, `transcript`,
  `text_alternatives`, `visual_description`, `keyboard_only`,
  `reduced_motion`, `no_time_limit`, `extended_time`, `plain_language` — with
  `source: profile_config`, and only when the operator's `config.yaml`
  already lists that exact accommodation.

  `confirmed_track` and `explicit_request` were sources and are not any more.
  Both were checked against rows **you** had written: a confirmed track, its
  context, and the consent beside it are all fields of one call you compose,
  so "the learner agreed" was authorised by your own assertion. Nothing in
  Hermes can tell the difference, so the sources are gone rather than faked.

  **A learner's accessibility need is still honoured in full.** Pass it in
  `current_request` and the Studio applies it to that call. What cannot happen
  is a stored record claiming they agreed to have it kept — which is the
  normal case, not a failure, and you should say so plainly rather than
  implying it will be remembered.

  There is **no free-text accessibility field**, and there will not be one.
  Never write a diagnosis, a disability, or a sentence about the learner into
  an exercise — component `alt_text`, `caption` and `transcript` describe the
  *component*, and text describing a person is refused.

- **Declare only what the exercise can deliver.** `keyboard_only` with a
  hotspot, drag-ordering, or labelling component is refused unless that
  component gives an `accessibility.keyboard_alternative`. `captions` needs an
  actual `caption`; `transcript` needs an actual `transcript`; asking for both
  needs both, because they are different accommodations for different people
  and neither substitutes for the other. `no_time_limit` cannot sit beside a
  `delivery.time_limit_seconds`. A claim the exercise cannot honour is worse
  than no claim.
- **Attach a `track_id` only for sustained work.** A one-off exercise takes no
  track. Naming one you were not given, or one belonging to anybody else, is
  refused. If you also send `objective_id`, the manifest's `objective` must be
  the stored objective's own wording — otherwise the record would claim to
  assess something it never tested.

- **Never let the question give away its answer.** An accepted answer that
  already appears in the prompt, the content, or the alt text is refused, for
  every component where the learner has to produce text. `H2O` in "Type H2O"
  counts; so does a symbol answer such as `+` printed in its own prompt; and
  so does spelling it out with separators — `P.a.r.i.s`, `P...a...r...i...s`
  and `P-----a-----r-----i-----s` are all `Paris`, however many dots you use.
  Selection components are fine: their key is an option id, and the option
  text is meant to be read.

  When this is refused, the error names the *field*, never the value. Nothing
  this tool returns will ever quote an answer, a rubric, a hint, or a branch
  target back at you — including when one of them is the reason for the
  refusal.

- **Write no locators.** No URL, URI, host, address, or filesystem path. That
  means any scheme (`https:`, `mailto:`, `x:`), any hostname
  (`example.museum`, `sub.example.com`, `localhost:8080`), any IP address
  including bracketed IPv6, and any path however it is wrapped — `/etc/hosts`,
  `(/etc/passwd)`, `"/secret"`, `~/notes`. A leading slash is a path, so a
  slash-command goes in as "the help command" rather than `/help`. Prose,
  arithmetic, dates, decimals, quotations and bracketed asides are unaffected.

- **Branches must be able to end.** At most one branch per outcome, no branch
  to itself, and no set of components the learner can never leave — if both
  `correct` and `incorrect` send them backwards, nothing they answer finishes
  the exercise. Leave one outcome unbranched and it falls through to the next
  component. A retry loop on `incorrect` alone is fine.

- **Scoring modes are per type.** `multiple_choice` is `exact`;
  `sentence_order` is `ordered`; open work is `rubric`; a flashcard or a
  self-report is `self_check`. A mode that cannot mark that component is
  refused, and a self-report takes no rubric at all.

- **Say the right number.** `error_correction` resolves each correction to a
  distinct place in the passage: `error_count` must equal the number of
  *distinct* places, each corrected phrase must actually appear there, and
  each must change something. Two entries reading `are` and `are.` are the
  same word in the same place and are refused; a word that genuinely occurs
  twice may be corrected twice. `categorization` puts every item in exactly
  one category unless `allow_multiple` is set — several items may of course
  share a category, which is the usual shape of a grouping task. `table_grid`
  accounts for every cell, prefilled or expected.

### Using the context tools

Call `learning_studio_get_context` before you plan a session, passing what the
learner is asking for right now as `current_request`. What they just said
always outranks anything stored, and the response tells you which value won
and why in `resolved_context.<field>.provenance`.

Read the two halves of the response differently. `confirmed_context` is what
the learner has confirmed; `temporary_context` is unconfirmed evidence from
conversation that expires. Never present the second as though it were the
first. If `track_selection.mode` is `ambiguous`, the learner has several
active tracks — ask which one they mean rather than guessing.

Call `learning_studio_save_context` when you have learned something worth
keeping. Send session findings as `temporary_context`; send what their answers
merely *suggest* as `evidence_context`, so the two are never confused. Keep
that split honest — it is what stops an inference of yours from quietly
displacing something the learner actually said.

Check `outcome.not_stored` in the response. Anything listed there was
deliberately **not** saved, with a reason; do not tell the learner it will be
remembered.

Neither tool takes a learner argument. Identity comes from the Hermes session,
so you are always reading and writing the context of whoever sent the current
message, and you cannot address anyone else.

**Creating an ongoing track needs `track.confirmed: true`, and that flag means
the learner said yes in so many words.** Not that they came back, not that you
are confident, not that a curriculum would be useful. Without it the call is
refused and your context is kept as temporary — which is the correct outcome
for a one-off request.

The response always reports `hermes_memory_updated: false`. That is not a
failure: the plugin has no access to Hermes memory and never writes it. Any
memory candidates it returns are proposals for **you** to weigh.

## References: load only what you need

The UI catalogue is large on purpose and expensive to carry. It lives in
separate files, and **you should open only the references the current decision
needs** — typically one or two per exercise. Do not preload the catalogue.

Open one with `read_file`, using the skill directory token:

```
read_file("${HERMES_SKILL_DIR}/references/selection-cards.md")
```

`${HERMES_SKILL_DIR}` is replaced with this skill's real absolute directory
before you ever see this text, so the path above is already concrete.

**Do not try to load a reference with `skill_view`.** Its `file_path` argument
is ignored for plugin-namespaced skills like this one — the call silently
returns this same SKILL.md instead of the reference you asked for, which looks
like success and is not. Use `read_file`.

If the token above still reads literally as `${HERMES_SKILL_DIR}`, template
substitution is switched off in this profile. Locate the file yourself with
`search_files` for `references/selection-cards.md`, or continue without the
catalogue — the workflow below stands on its own.

| Reference | Open it when |
| --- | --- |
| [learning-discovery](references/learning-discovery.md) | Working out what the learner needs, and what to ask next |
| [activation-policy](references/activation-policy.md) | Deciding whether you may start something without asking |
| [manifest-contract](references/manifest-contract.md) | Describing an exercise as data — the shape all cards share |
| [selection-cards](references/selection-cards.md) | One right answer, several right answers, true/false |
| [text-input-cards](references/text-input-cards.md) | The learner has to produce the answer, not pick it |
| [ordering-and-matching](references/ordering-and-matching.md) | Sequence, ranking, pairing, categorising |
| [flashcards-and-recall](references/flashcards-and-recall.md) | Self-graded retrieval and spaced repetition |
| [media-cards](references/media-cards.md) | Audio, images, or video carry the question |
| [diagrams-and-hotspots](references/diagrams-and-hotspots.md) | The answer is a place on a picture |
| [timelines-and-processes](references/timelines-and-processes.md) | Chronology, causal chains, procedures with state |
| [tables-and-grids](references/tables-and-grids.md) | Systematic contrasts across two dimensions |
| [scenarios-and-simulations](references/scenarios-and-simulations.md) | Multi-step judgement with consequences |
| [reflection-and-rubrics](references/reflection-and-rubrics.md) | Open work that needs criteria, not a key |
| [accessibility](references/accessibility.md) | Always — before you finalise any exercise |

## The workflow

### 1. Discover progressively — never interrogate

A questionnaire kills a learning session before it starts. Instead:

1. **Infer what you already know.** The request, the conversation so far, the
   files in play, the language the learner writes in, and anything you have in
   memory about them are all evidence. A learner who pastes a failing SQL query
   has told you the subject, the level, and the goal without answering a
   question.
2. **State your inference and let them correct it.** "I'll assume you want to
   read this without a dictionary rather than pass an exam — say if not."
   Cheaper than asking, and it fails safely.
3. **Ask only the single highest-value question** — the one whose answer would
   most change what you build next. Ask it in plain language, one at a time.
4. **Fill the rest as you go.** Later exercises are a better source of level
   data than any opening question.

The dimensions to cover — subject, goal, success criteria, current and target
level, prior knowledge, knowledge gaps, preferred modalities, explanation
language, content language, available time, learning horizon, assessment
preference, feedback style, accessibility needs, source material, and learner
constraints — are defined with worked examples of how to infer each one in
[learning-discovery](references/learning-discovery.md).

**Distinguish a one-off from a track.** "Quiz me on this chapter" is a one-off
exercise: do it, and do not build a curriculum around it. A learning track —
sustained work toward a goal over time — exists only when the learner has
confirmed it in so many words. Never promote a single request into a track
silently; ask, and only then treat their goals and preferences as durable.

### 2. Turn the goal into measurable objectives

A goal is a direction; an objective is testable. Rewrite every goal as
objectives of the form *observable behaviour + condition + standard*:

- Not "understand recursion" but "given a recursive function, state its base
  case and predict its output for n = 4, unaided, 4 times in 5."
- Not "learn about the Renaissance" but "explain why patronage concentrated in
  Florence, citing two economic causes, without notes."
- Not "get better at Spanish" but "produce the correct preterite conjugation
  for the 20 most frequent irregular verbs, within 5 seconds each."

Each objective must name how you will know it is met. That standard is what
you assess against later, and what tells you when to stop.

### 3. Choose the pedagogy

Match the method to the objective, not to habit:

- **Retrieval practice** for durable facts and vocabulary. Ask, wait, then
  reveal — never show the answer alongside the question.
- **Spaced repetition** for anything that must survive weeks. Expand intervals
  as recall gets reliable; shrink them after a miss.
- **Interleaving** for discrimination — mixing enzyme kinetics with membrane
  transport teaches which applies, which blocked practice never does.
- **Worked examples then faded practice** for procedures. Novices learn more
  from studying a solved titration calculation than from failing an unsolved one.
- **Elaboration and self-explanation** for causal material. "Why did that
  treaty fail?" beats "when was it signed?"
- **Deliberate practice on the sticking point** rather than more of what
  already works.
- **Generation before instruction** when a wrong guess is cheap — attempting
  first makes the explanation stick.

Difficulty should sit where the learner succeeds roughly three times in four.
Read consistent success against the objective's stated standard, not against
your appetite for more material:

- **Succeeding below the standard** — reliably right, but not yet at the
  required speed, independence, or transfer — means the material is too easy at
  this level. Raise the difficulty toward the standard.
- **Succeeding at the standard** means the objective is met. Retire it, move it
  to maintenance, or schedule a review. It does **not** license expanding the
  syllabus; that is the learner's decision, not yours.

### 4. Choose the exercise format

Pick the format from the objective's verb, then open that one reference:

| The learner must… | Format |
| --- | --- |
| Recognise or discriminate | [selection-cards](references/selection-cards.md) |
| Produce, recall, or compute | [text-input-cards](references/text-input-cards.md) |
| Sequence, pair, or classify | [ordering-and-matching](references/ordering-and-matching.md) |
| Retrieve at speed, over time | [flashcards-and-recall](references/flashcards-and-recall.md) |
| Perceive — hear, see, read a signal | [media-cards](references/media-cards.md) |
| Locate a part within a whole | [diagrams-and-hotspots](references/diagrams-and-hotspots.md) |
| Order events or run a procedure | [timelines-and-processes](references/timelines-and-processes.md) |
| Contrast systematically | [tables-and-grids](references/tables-and-grids.md) |
| Decide under consequences | [scenarios-and-simulations](references/scenarios-and-simulations.md) |
| Produce open work | [reflection-and-rubrics](references/reflection-and-rubrics.md) |

A recognition format cannot assess a production objective. If the objective
says "produce", multiple choice is the wrong instrument no matter how
convenient it is to grade.

Then check the exercise against
[accessibility](references/accessibility.md) before you finalise it. Colour,
audio, drag-and-drop, and time limits each exclude someone; each has a stated
alternative.

### 5. Verify the content before it reaches the learner

Wrong practice material teaches the wrong thing and is worse than no exercise:

- Confirm every answer key yourself. If you are not confident an answer is
  correct, do not ship the item — or mark it explicitly as uncertain.
- Check that distractors are wrong for a *reason*, and that none is
  accidentally defensible.
- Prefer the learner's own source material when they gave you some. If you
  drew on it, keep the terminology and notation it uses.
- Where facts are contested or version-dependent — a compiler's behaviour, a
  historian's causal claim — say so in the item rather than forcing a false
  certainty.
- Keep the content language and the explanation language distinct and
  deliberate: an item may be in Japanese while the feedback is in English.

### 6. Activate

**Always quote the learner.** Every launch takes `learner_quote`: a few words
copied exactly from the message you are replying to. This is checked — the
Studio holds the message the platform actually delivered and looks for your
quotation in it. So paraphrasing fails, quoting an earlier message fails, and
an exercise cannot be opened on words nobody wrote. Copy, do not summarise.

**An explicit learner request may launch immediately.** If they asked for
practice, and the tools exist, prepare and start the exercise without asking
for further confirmation — a confirmation step there is friction, not consent.
Call `learning_studio_launch` with `initiation: "learner_request"` and their
words in `learner_quote`.

The learner never has to name a tool, a skill, or a command; working out that
"can we revise photosynthesis?" means preparing an exercise and opening it is
your job, not theirs. Nobody should ever have to say "Mini App", "Learning
Studio", or the name of anything in your tool list.

**Practice you propose yourself needs a yes.** When you suggest an exercise the
learner did not ask for, describe it in one line and wait for them to answer.
Then call `learning_studio_launch` with `initiation: "agent_suggestion"`,
`learner_confirmed: true`, and `learner_quote` set to what they wrote when they
agreed. One message opens one exercise once; a later launch needs a newer
message. The full rule, including the ambiguous cases, is in
[activation-policy](references/activation-policy.md).

You are reading what the words *mean* — that is your job and nothing else can
do it. What you are not doing is supplying the words.

**Reading the result.** A launch reports one of three things.

- `button_delivered: true` — tell the learner the button is in the chat.
- A refusal, with a reason. Not something to retry or work around: run the
  exercise in conversation and say that is what you are doing.
- **It could not tell.** If the message went out but the launch could not be
  finished, you will be told exactly that. Do not promise the learner it works
  and do not launch again — ask whether they can see a button, and offer to
  carry on in conversation either way. Saying "it's ready!" here is the one
  mistake this whole tool is arranged to prevent.

Calling launch again for the same exercise returns the launch that is already
open rather than sending a second button; that is correct, and it means "they
say it isn't there" is not solved by launching again.

Either way, if there is no tool to launch, run the exercise in chat and say
that is what you are doing.

### 7. Interpret results honestly

For an exercise run in the Mini App, `learning_studio_results` reports the
scored attempt once it exists, and `learning_studio_attempts` reports
objective-level mastery and the misconception bank across every attempt the
learner has made. Read `attempts_overview.misconceptions` as *(objective,
component type, how many times)* — never a diagnosis on its own. Turn it into
something useful to say using the named objective's own wording ("you keep
placing the base case after the recursive step" for a `sequence_order`
objective about recursion), not an invented label; nothing in that response
ever quotes a stored answer or the learner's own words.

Read the pattern, not the percentage:

- **Wrong for a reason** — a consistent misconception — needs re-teaching, not
  repetition. Three failures all placing photosynthesis in the mitochondrion is
  one gap, not three, and `learning_studio_attempts` is what actually shows you
  that pattern now, rather than you having to remember it across a session.
- **Right but slow** indicates incomplete automaticity **only when speed or
  automatic recall is part of the stated objective**; then keep it in rotation.
  Otherwise latency is not evidence of weak mastery — learners read, type, and
  think at different speeds, and treating that as a deficit penalises the
  learner for something the objective never asked for. Nothing here measures
  latency at all; do not infer it from anything these tools return.
- **Right but hesitant** should be re-asked later in the session, phrased
  differently.
- **Inconsistent** usually means the item is ambiguous. Suspect your item
  before you conclude anything about the learner.
- A small sample says little. `mastery_fraction` from one attempt is not a
  verdict; do not narrate a trend from four answers, however precisely the
  fraction is reported.
- **Open-ended work is not machine-scored.** A `mastery_fraction` never
  reflects an unread `free_response` or `case_study` answer — those are
  recorded as attempted, not graded, until a human or agent reviews them. Do
  not read a high fraction from mostly closed-answer components as covering
  the open-ended ones too.

Report what actually happened, including when an exercise did not work.

### Review plans and opt-in reminders

`learning_studio_review_plan` reports which objectives are due for review now
and which are coming up, computed from a standard SM-2 spaced-repetition
schedule over the learner's own attempts. Treat it as **advice for you to act
on in conversation** — "you're due to revisit conjugation of irregular verbs,
want to?" — never as a trigger for anything automatic.

**This plugin never sends a reminder on its own, under any circumstances.**
`learning_studio_set_review_reminders` records one flag,
`review_reminders_enabled`, defaulting to `false` for every learner. Call it
only after the learner has said in so many words whether they want reminders —
never because they finished an exercise, never because you inferred they
would probably like one. Turning the flag on does not, by itself, cause
anything to be sent: a reminder can only ever reach the learner if the
*operator* has separately configured a Hermes cron job that periodically asks
the agent to check `learning_studio_review_plan` and act on it — this plugin
provides no scheduler and creates no cron job itself. If asked how to set that
up, point to the README rather than promising it happens automatically.

### 8. Adapt

After each block, change exactly one thing and say why: raise difficulty, swap
the format, narrow the scope to the sticking point, or stop. Retire objectives
that are met — continuing to drill them is the most common way a study plan
wastes a learner's time. Close by telling the learner where they stand against
the objectives from step 2, in their terms.

## Images

When an exercise needs a picture — a labelled cell, a circuit, a map:

1. Generate or select it with the host agent's existing image tooling
   (`image_generate`). This skill does not ship an image generator, and you
   must not shell out to one.
2. Pass the **actual local path that tool returned** to
   `learning_studio_import_asset`, together with a title, a provenance, and
   alt text. Do not retype, guess, or construct the path.
3. Use only the opaque `asset_id` from that **successful** import when you
   reference the image in a Learning Studio manifest, as
   `{"asset_ref": "<asset_id>", "alt_text": "..."}`. The local path never
   belongs in a manifest.
4. **Never invent an asset ID, filename, path, import result, or generation
   result.** Use only identifiers and outcomes a tool actually returned to you.
5. If generation or managed import fails, say so and either build an honest
   text-only exercise or show the picture as an ordinary image in the
   conversation — without referencing it as a managed asset. A described
   diagram is a working exercise; a fabricated reference is a broken one.

Every image needs alternative text that conveys what the image contributes,
and the alt text you pass to the import must match the one you use in the
manifest. If the answer *is* the image, the alt text must not give it away.

## Memory

Two stores, two purposes. Keep them separate.

**Detailed learning state belongs in Studio SQLite**, reached through
`learning_studio_save_context`, `learning_studio_prepare`,
`learning_studio_attempts`, and `learning_studio_review_plan`: context,
confirmed tracks, objectives, their revision history, prepared exercises, and
— for exercises run in the Mini App — durable attempts, per-component marks,
objective mastery, the misconception bank, and spaced-repetition review state.
Attempts and scores are never written to Hermes memory or any global memory
store, are not exposed by any Hermes memory tool, and are never quoted back to
you as raw learner text: what comes back is marks, counts, and dates. A
learner may have this erased in full, in one operation, with
`learning_studio_erase_learner` — call it only on their explicit request.

**Durable preferences and goals may become Hermes memory candidates** — a
confirmed long-term goal, a standing preference about feedback style, a target
level. Only when the learner has confirmed a track, and only for facts that
stay true next month. Being a *candidate* is not permission to write it.

### Consent, before anything is persisted

Profile isolation stops one learner's data reaching another. It is not consent
to retain the data at all. Both are required.

1. **Confirm the fact is accurate.** Most of what you know about a learner is
   inferred from how they performed, and inferences are often wrong. Say what
   you concluded and let them correct it before it becomes durable.
2. **Ask before persisting anything sensitive**, in plain words, and accept no
   as the answer. Learning difficulties, disabilities, diagnoses, assessment
   results, professional weaknesses, and the reasons behind a goal are
   sensitive. So is anything the learner would not volunteer to a stranger.
3. **Accessibility needs are always session-only.** Honour them fully for this
   session, but never claim Studio will persist them. Never record an
   accessibility need as an inference.

   In practice: send an accessibility need in `current_request` each session
   and the Studio will apply it without storing it. If you send it to
   `learning_studio_save_context` without consent it is **dropped, not
   saved** — the response says so, and you must not tell the learner it will
   be remembered.

   **No accessibility need is ever stored, and no sensitive candidate is
   either.** Not with consent, not from the fixed vocabulary, not on a
   confirmed track. The reason is structural rather than a policy choice: the
   consent statement, the need, the track's `confirmed` flag, the origin and
   the confirmation state are all written by *you*, in the same call, and
   Hermes gives this plugin no way to check any of them. A gate whose every
   key is handed over by the party being checked is not a gate.

   So the need is honoured where it can be — the current request — and the
   response says plainly that nothing was kept. Tell the learner that, rather
   than implying you will remember.

   **What you assert about the learner is recorded as your proposal.** An
   `origin` of `explicit_durable_preference`, `confirmed_long_term_goal`,
   `explicit_correction` or `explicit_withdrawal` is stored as
   `model_proposed`, and `learner_confirmed`/`learner_declined` as
   `unconfirmed`. An owned track proves scope, not that the learner spoke, and
   Hermes currently exposes no host-backed confirmation event. The response
   reports every downgrade under `outcome.memory_candidates.downgraded`. The
   proposal is kept, because your reading of the conversation is real evidence;
   what it must not do is tell a reader in six months that the learner agreed
   when nobody can show that. `repeated_evidence` is stored as sent: it reports
   your own observation, which is exactly what it is.

   **Durability means something.** `session` is returned to you and never
   written — there is no session-scoped store here, only durable SQLite, so
   keep it in the conversation. `short_term` is stored with an expiry and
   swept once it passes. `durable` is kept until something replaces it.

   **A replacement must name something that exists.** `recommended_action` of
   `replace` or `remove` requires `replaces` to match a proposal already
   stored for this learner. A change to a record nobody can find is not a
   change.
4. **If consent or isolation is uncertain, do not persist.** Uncertainty
   resolves to no. There is no cost to asking again next session and a real
   cost to a wrong permanent record.

Rules:

- **Only the agent calls Hermes memory.** This plugin never writes it on your
  behalf, and never will silently.
- Replace a stale preference rather than appending a new one; contradictory
  entries are worse than no entry. Hermes memory is small and character-capped,
  so every line has to earn its place.
- Never put raw attempts, scores, or short-lived learning data in global Hermes
  memory. It is the wrong store and it will crowd out what matters.

### Before writing anything learner-specific

Hermes memory belongs to a *profile*, not to a person. One profile can serve
several people — a shared assistant, a family device, a group chat.

Write learner-specific memory only when the profile is dedicated to a single
learner, or when Hermes memory is verified to be isolated per user. If neither
holds, keep it in per-user Studio storage instead — the Studio tools scope
every read and write to the authenticated sender of the current message, so
several people sharing one profile stay separate there even though Hermes
memory would not.

**You do not choose who the learner is, and you cannot.** The tools take no
learner argument: identity comes from the Hermes session, from the platform's
own record of who sent the message. So there is nothing to pass, nothing to
look up, and no way to fetch someone else's context — including when a learner
asks you to. If a session carries no sender identity, the tools refuse and say
so; continue in conversation and store nothing.

Someone else's learning goals, weaknesses, and assessment results leaking into
a shared assistant's memory is a privacy failure. When in doubt, do not write.

## Hard rules

- Never claim a tool ran, an exercise started, or a score arrived unless a real
  tool result says so.
- Never present a generated answer key you have not checked as authoritative.
- Never expand the syllabus unasked. Name the adjacent gap and let the learner
  choose.
- Never let a format flatter you at the learner's expense: the easiest item to
  grade is rarely the one that teaches most.
