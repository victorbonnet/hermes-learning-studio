/*
 * The whole application, against a fake authenticated API.
 *
 * The fake speaks the real routes and the real response bodies -- the shapes are
 * pinned on the Python side by `tests/test_mini_app_api.py`, and the components
 * it serves are the generated learner projections, so a card that renders here
 * renders against what the server actually sends.
 *
 * What each test is really checking is the unhappy path as much as the happy
 * one. A frontend that only handles success is a frontend that shows a spinner
 * forever the first time a session expires, and "sessions are short by design"
 * makes that not an edge case but a certainty.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { click } from "./dom.mjs";
import { completeCard } from "./complete.mjs";
import { canaryPrefix, loadApp, payloads, settle } from "./harness.mjs";

const FIXTURES = payloads();
const CANARY = canaryPrefix();
const ALL_TYPES = Object.keys(FIXTURES).sort();
const FLASHCARD_BACK = "yama";

const INIT_DATA_HEADER = "X-Telegram-Init-Data";
const SESSION_HEADER = "X-Learning-Studio-Session";

const EXPERIENCE_ID = "exp-abc123";
// The selector the runtime issues and the button carries. Shaped exactly as
// `LAUNCH_ID_PATTERN` in `learning_studio/runtime/grants.py`.
const LAUNCH_ID = "Kx7vQm2ZpL9dR4sT";
const INIT_DATA = `auth_date=1800000000&hash=deadbeef`;

/** The address the real button opens, as the sender builds it. */
function buttonLocation(launchId = LAUNCH_ID) {
  return { search: "", pathname: "/", hash: `#launch=${launchId}` };
}

function telegramStub(overrides = {}) {
  const calls = {
    ready: 0,
    expand: 0,
    close: 0,
    closingConfirmation: 0,
    events: [],
    handlers: {},
  };
  return Object.assign(
    {
      initData: INIT_DATA,
      initDataUnsafe: {},
      colorScheme: "light",
      themeParams: { bg_color: "#101010", text_color: "#f0f0f0" },
      calls,
      ready() {
        calls.ready += 1;
      },
      expand() {
        calls.expand += 1;
      },
      close() {
        calls.close += 1;
      },
      enableClosingConfirmation() {
        calls.closingConfirmation += 1;
      },
      onEvent(name, handler) {
        calls.events.push(name);
        calls.handlers[name] = handler;
      },
    },
    overrides
  );
}

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  };
}

/**
 * An API that behaves like the real one for one experience.
 *
 * `failWith` injects a status for the *next* request to a given path, which is
 * how the error states are reached without a second fake.
 */
function fakeApi({
  types = ["multiple_choice"],
  uiLocale = "en",
  contentLocale = "en",
  failWith = {},
  summaryOverrides = {},
} = {}) {
  const components = types.map((componentType, index) =>
    Object.assign({}, FIXTURES[componentType], {
      position: index,
      component_id: `component-${index}`,
    })
  );
  const experience = {
    experience_id: EXPERIENCE_ID,
    title: "A short practice set",
    instructions: "Work through each item in order.",
    ui_locale: uiLocale,
    content_locale: contentLocale,
    expected_duration_minutes: 12,
    difficulty: "intermediate",
    accessibility: {},
    component_count: components.length,
  };

  const log = [];
  const answers = [];
  const reveals = new Map();
  let token = "";
  let position = 0;

  function progress() {
    return {
      position: position,
      component_count: components.length,
      answered: answers.length,
      completed: position >= components.length,
    };
  }

  function componentAt(index) {
    return index < components.length ? components[index] : null;
  }

  async function fetchImpl(path, init) {
    const entry = { path, method: init.method, headers: init.headers, body: init.body };
    log.push(entry);

    const injected = failWith[path];
    if (injected !== undefined) {
      delete failWith[path];
      if (injected === "network") {
        throw new Error("no connection");
      }
      return jsonResponse(injected, { error: "refused" });
    }

    if (path === "/api/session" && init.method === "POST") {
      token = "session-token-value";
      return jsonResponse(201, {
        session_token: token,
        expires_in_seconds: 1800,
        experience,
        progress: progress(),
        limits: { max_response_chars: 4000, max_request_bytes: 16384 },
      });
    }
    if (path === "/api/session/component") {
      return jsonResponse(200, { progress: progress(), component: componentAt(position) });
    }
    if (path === "/api/session/reveal") {
      // Mirrors the real route: current card only, attempt required, attempt
      // frozen on the first call.
      const sent = JSON.parse(init.body);
      const current = componentAt(position);
      if (!current || sent.component_id !== current.component_id) {
        return jsonResponse(409, { error: "not the current question" });
      }
      if (typeof sent.attempt !== "string" || !sent.attempt.trim()) {
        return jsonResponse(400, { error: "no attempt" });
      }
      if (!reveals.has(current.component_id)) {
        reveals.set(current.component_id, sent.attempt);
      }
      return jsonResponse(200, {
        component_id: current.component_id,
        revealed: true,
        back: FLASHCARD_BACK,
        attempt: reveals.get(current.component_id),
        notice: "Nothing has been marked.",
      });
    }
    if (path === "/api/session/answer") {
      const sent = JSON.parse(init.body);
      const current = componentAt(position);
      if (!current || sent.component_id !== current.component_id) {
        return jsonResponse(409, { error: "not the current question" });
      }
      if (current.type === "flashcard") {
        const frozen = reveals.get(current.component_id);
        if (frozen === undefined) {
          return jsonResponse(409, { error: "reveal required" });
        }
        if (!sent.response || sent.response.text !== frozen) {
          return jsonResponse(409, { error: "recall changed after reveal" });
        }
      }
      answers.push(sent);
      position += 1;
      return jsonResponse(200, {
        recorded: true,
        scored: false,
        progress: progress(),
        next_component: componentAt(position),
        notice: "Responses are recorded for this session only.",
      });
    }
    if (path === "/api/session/result") {
      return jsonResponse(200, {
        experience_id: EXPERIENCE_ID,
        title: experience.title,
        progress: progress(),
        scored: false,
        answered_components: answers.map((answer) => answer.component_id),
        notice: "Nothing has been marked.",
      });
    }
    if (path === "/api/session/summary") {
      // Mirrors the shape `service.record_attempt` returns: every answered
      // component gets a mark component-by-component, and the fake marks the
      // first one right and the rest wrong so a test can tell them apart.
      const scoredComponents = answers.map((answer, index) => ({
        component_id: answer.component_id,
        component_type: componentAt(index) ? componentAt(index).type : "unknown",
        graded: true,
        correct: index === 0,
        score: index === 0 ? 1 : 0,
        max_score: 1,
        feedback: null,
      }));
      // `summaryOverrides` replaces individual marks so a test can reach a
      // tier or a boundary the walk-through cannot produce on its own. An
      // empty override — the default — leaves the shape above untouched.
      return jsonResponse(
        200,
        Object.assign(
          {
            experience_id: EXPERIENCE_ID,
            attempt_id: "attempt-fake",
            scored: true,
            overall_score: scoredComponents.length ? 1 : 0,
            overall_max_score: scoredComponents.length,
            graded_component_count: scoredComponents.length,
            correct_component_count: scoredComponents.length ? 1 : 0,
            component_count: components.length,
            components: scoredComponents,
            review: null,
            notice: "Scored from this attempt.",
          },
          summaryOverrides
        )
      );
    }
    if (path.startsWith("/api/assets/")) {
      return {
        ok: true,
        status: 200,
        json: () => Promise.resolve({}),
        blob: () => Promise.resolve({ type: "image/png", size: 12 }),
      };
    }
    return jsonResponse(404, { error: "no such route" });
  }

  return { fetchImpl, log, answers, reveals, components, experience, progress, failWith };
}

async function boot(options = {}) {
  const api = fakeApi(options);
  const telegram = options.telegram === null ? null : telegramStub(options.telegram);
  const { win, booted } = loadApp({
    telegram,
    fetch: api.fetchImpl,
    location: options.location === undefined ? buttonLocation() : options.location,
  });
  await booted;
  await settle(20);
  return { win, api, telegram, node: (id) => win.document.getElementById(id) };
}

function cardOf(win) {
  return win.document.getElementById("card");
}

function stateOf(win) {
  const card = cardOf(win);
  const marked = card.all().find((node) => node.getAttribute("data-state"));
  return marked ? marked.getAttribute("data-state") : null;
}

/** Answer whatever card is showing and continue past the confirmation. */
async function answerCurrent(context) {
  const win = context.win;
  const card = cardOf(win);
  const section = card.children[0];
  const componentType = section.getAttribute("data-component-type");
  const payload = componentType ? FIXTURES[componentType].payload : {};

  completeCard(card, payload);
  // A flashcard's turn-over is a request; give it a chance to come back before
  // the answer is submitted.
  await settle(20);
  click(context.node("primary-action"));
  await settle(20);

  assert.equal(stateOf(win), "recorded", `${componentType}: no confirmation after submitting`);
  click(context.node("primary-action"));
  await settle(20);
  return componentType;
}

// ── The happy path ────────────────────────────────────────────────────────

test("a launch from Telegram opens a session and shows the first card", async () => {
  const context = await boot({ types: ["multiple_choice", "short_answer"] });

  assert.equal(context.telegram.calls.ready, 1);
  assert.equal(context.telegram.calls.expand, 1);
  assert.equal(context.telegram.calls.closingConfirmation, 1);
  assert.deepEqual(context.telegram.calls.events, ["themeChanged"]);

  assert.equal(context.api.log[0].path, "/api/session");
  // The selector from the button's fragment, and nothing else. The page never
  // names an experience: the server derives that from the grant.
  const bootstrap = JSON.parse(context.api.log[0].body);
  assert.equal(bootstrap.launch_id, LAUNCH_ID);
  assert.equal(bootstrap.experience_id, undefined);
  assert.equal(context.node("exercise-title").textContent, "A short practice set");
  assert.match(context.node("progress-text").textContent, /Card 1 of 2/);
  assert.equal(
    cardOf(context.win).children[0].getAttribute("data-component-type"),
    "multiple_choice"
  );
});

test("the exercise runs to completion, one deliberate tap at a time", async () => {
  const context = await boot({ types: ["multiple_choice", "true_false", "short_answer"] });

  const walked = [];
  walked.push(await answerCurrent(context));
  walked.push(await answerCurrent(context));
  walked.push(await answerCurrent(context));

  assert.deepEqual(walked, ["multiple_choice", "true_false", "short_answer"]);
  assert.equal(context.api.answers.length, 3);
  assert.deepEqual(
    context.api.answers.map((answer) => answer.component_id),
    ["component-0", "component-1", "component-2"]
  );
  assert.equal(stateOf(context.win), "complete");
  assert.match(cardOf(context.win).textContent, /Answered: 3 of 3/);
  assert.match(cardOf(context.win).textContent, /You got 1 of 3 marked answers right/);
});

test("the completion screen shows a per-component breakdown from the summary route", async () => {
  const context = await boot({ types: ["multiple_choice", "true_false", "short_answer"] });

  await answerCurrent(context);
  await answerCurrent(context);
  await answerCurrent(context);

  const card = cardOf(context.win);
  assert.equal(card.children[0].getAttribute("data-state"), "complete");

  const items = card.all().filter((node) => node.getAttribute && node.getAttribute("data-outcome"));
  assert.equal(items.length, 3);
  assert.deepEqual(
    items.map((node) => node.getAttribute("data-outcome")),
    ["correct", "incorrect", "incorrect"]
  );
});

test("the completion screen adds a short feedback paragraph under the score", async () => {
  const context = await boot({ types: ["multiple_choice", "true_false", "short_answer"] });

  await answerCurrent(context);
  await answerCurrent(context);
  await answerCurrent(context);

  const text = cardOf(context.win).textContent;
  assert.match(text, /You got 1 of 3 marked answers right/);
  // One of three correct is a third of the points: the "tricky" tier, plus a
  // review count naming the two missed marks.
  assert.match(text, /tricky exercise/i);
  assert.match(text, /2 question\(s\) to review/);
});

test("a perfect score gets the encouraging tier and no review count", async () => {
  // The fake summary marks the first component right and the rest wrong, so a
  // single-component exercise is the way to reach a perfect mark.
  const only = await boot({ types: ["multiple_choice"] });
  await answerCurrent(only);

  const text = cardOf(only.win).textContent;
  assert.match(text, /You got 1 of 1 marked answers right/);
  assert.match(text, /Excellent work/i);
  assert.doesNotMatch(text, /question\(s\) to review/);
});

/** Run a one-card exercise to its completion screen with the marks rewritten. */
async function finishWithMarks(summaryOverrides) {
  const context = await boot({ types: ["multiple_choice"], summaryOverrides });
  await answerCurrent(context);
  return context;
}

test("the feedback tier follows the marked-answer count at every threshold", async () => {
  const cases = [
    { correct: 9, graded: 10, expect: /Excellent work/i }, // 0.9 exactly
    { correct: 8, graded: 9, expect: /Well done/i }, // 0.888, just under 0.9
    { correct: 2, graded: 3, expect: /Well done/i }, // 0.667
    { correct: 3, graded: 5, expect: /Well done/i }, // 0.6 exactly
    { correct: 5, graded: 9, expect: /foundations are there/i }, // 0.555, just under 0.6
    { correct: 2, graded: 5, expect: /foundations are there/i }, // 0.4 exactly
    { correct: 3, graded: 8, expect: /tricky exercise/i }, // 0.375, just under 0.4
    { correct: 0, graded: 4, expect: /tricky exercise/i }, // nothing right
  ];

  for (const item of cases) {
    const context = await finishWithMarks({
      correct_component_count: item.correct,
      graded_component_count: item.graded,
      component_count: item.graded,
    });
    const text = cardOf(context.win).textContent;
    assert.match(text, item.expect, `${item.correct} of ${item.graded}`);
    assert.match(
      text,
      new RegExp(`${item.graded - item.correct} question\\(s\\) to review`),
      `${item.correct} of ${item.graded}: review count`
    );
  }
});

test("the feedback tier ignores a points weighting the counts never show", async () => {
  // Three of four marked answers right, but one heavily weighted item drags the
  // points to 3 of 8. The sentence has to agree with the count above it.
  const context = await finishWithMarks({
    correct_component_count: 3,
    graded_component_count: 4,
    component_count: 4,
    overall_score: 3,
    overall_max_score: 8,
  });

  const text = cardOf(context.win).textContent;
  assert.match(text, /You got 3 of 4 marked answers right/);
  assert.match(text, /Well done/i);
  assert.doesNotMatch(text, /tricky exercise/i);
});

test("nothing machine-graded gets no feedback paragraph at all", async () => {
  const context = await finishWithMarks({
    scored: false,
    overall_score: 0,
    overall_max_score: 0,
    graded_component_count: 0,
    correct_component_count: 0,
    components: [],
  });

  const text = cardOf(context.win).textContent;
  assert.match(text, /Nothing here is machine-graded/);
  assert.doesNotMatch(text, /Excellent work|Well done|foundations are there|tricky exercise/i);
  assert.doesNotMatch(text, /question\(s\) to review/);
  assert.doesNotMatch(text, /marked answers right/);
});

test("the completion announcement carries the feedback sentence, not just the title", async () => {
  const context = await finishWithMarks({
    correct_component_count: 2,
    graded_component_count: 3,
    component_count: 3,
  });

  const announced = context.node("announcer").textContent;
  assert.match(announced, /Exercise finished/);
  assert.match(announced, /Well done/i);
  assert.match(announced, /1 question\(s\) to review/);
});

test("the completion announcement is the title alone when nothing was graded", async () => {
  const context = await finishWithMarks({
    scored: false,
    overall_score: 0,
    overall_max_score: 0,
    graded_component_count: 0,
    correct_component_count: 0,
    components: [],
  });

  assert.equal(context.node("announcer").textContent, "Exercise finished");
});

test("every renderer survives a real submit, for all thirty-one types", async () => {
  const context = await boot({ types: ALL_TYPES });

  const walked = [];
  for (let index = 0; index < ALL_TYPES.length; index += 1) {
    walked.push(await answerCurrent(context));
  }

  assert.deepEqual(walked, ALL_TYPES);
  assert.equal(context.api.answers.length, ALL_TYPES.length);
  for (const answer of context.api.answers) {
    assert.ok(answer.response !== undefined && answer.response !== null);
  }
  assert.equal(stateOf(context.win), "complete");
});

test("the confirmation says the answer was recorded and not marked", async () => {
  const context = await boot({ types: ["short_answer", "true_false"] });
  const card = cardOf(context.win);

  completeCard(card, FIXTURES.short_answer.payload);
  click(context.node("primary-action"));
  await settle(20);

  assert.match(cardOf(context.win).textContent, /Answer recorded/);
  assert.match(cardOf(context.win).textContent, /Not marked yet/);
  assert.match(context.node("announcer").textContent, /recorded/i);
});

test("no state leaves an uninterpolated placeholder on screen", async () => {
  // The completion body counts what was answered, so it has placeholders in it.
  // Rendering the sentence once with the values and once without is an easy
  // mistake to make and an easy one to miss by eye.
  const context = await boot({ types: ["true_false", "short_answer"] });
  await answerCurrent(context);
  await answerCurrent(context);

  assert.equal(stateOf(context.win), "complete");
  const shown = context.win.document.documentElement.textContent;
  assert.ok(!/\{[a-z_]+\}/.test(shown), `an unfilled placeholder is showing: ${shown}`);
  assert.match(shown, /Answered: 2 of 2/);
});

test("closing the finished exercise goes through Telegram", async () => {
  const context = await boot({ types: ["true_false"] });
  await answerCurrent(context);

  click(context.node("primary-action"));

  assert.equal(context.telegram.calls.close, 1);
});

// ── Requests carry what they must, and nothing they must not ──────────────

test("every request carries the initData header and only the API paths are called", async () => {
  const context = await boot({ types: ["image_observation"] });
  await settle(20);

  assert.ok(context.api.log.length >= 2);
  for (const entry of context.api.log) {
    assert.equal(entry.headers[INIT_DATA_HEADER], INIT_DATA);
    assert.ok(entry.path.startsWith("/api/"), `${entry.path} is not an API path`);
    assert.ok(!entry.path.includes(INIT_DATA), "initData reached a URL");
    assert.ok(!entry.path.includes("?"), "a request carried a query string");
  }
});

test("the bootstrap sends no session header and later requests do", async () => {
  const context = await boot({ types: ["true_false"] });

  const bootstrap = context.api.log.find((entry) => entry.path === "/api/session");
  const later = context.api.log.find((entry) => entry.path === "/api/session/component");

  assert.equal(bootstrap.headers[SESSION_HEADER], undefined);
  assert.equal(later.headers[SESSION_HEADER], "session-token-value");
});

test("a managed image is fetched through the API and shown from a blob", async () => {
  const context = await boot({ types: ["image_observation"] });
  await settle(20);

  const assetRef = FIXTURES.image_observation.payload.content.image.asset_ref;
  const request = context.api.log.find((entry) => entry.path.startsWith("/api/assets/"));

  assert.equal(request.path, `/api/assets/${encodeURIComponent(assetRef)}`);
  assert.equal(request.headers[SESSION_HEADER], "session-token-value");
  assert.equal(request.headers[INIT_DATA_HEADER], INIT_DATA);

  const image = cardOf(context.win).byTag("img")[0];
  assert.match(image.getAttribute("src"), /^blob:/);
  assert.equal(context.win.objectUrls.created.length, 1);
});

test("blob urls are released when the card changes", async () => {
  const context = await boot({ types: ["image_observation", "true_false"] });
  await settle(20);

  assert.equal(context.win.objectUrls.created.length, 1);
  await answerCurrent(context);

  assert.ok(context.win.objectUrls.revoked.length >= 1, "an object url was leaked");
});

test("a response that is not an image is refused rather than shown", async () => {
  const api = fakeApi({ types: ["image_observation"] });
  const original = api.fetchImpl;
  const fetchImpl = async (path, init) => {
    if (path.startsWith("/api/assets/")) {
      return {
        ok: true,
        status: 200,
        json: () => Promise.resolve({}),
        blob: () => Promise.resolve({ type: "text/html", size: 9 }),
      };
    }
    return original(path, init);
  };
  const { win, booted } = loadApp({
    telegram: telegramStub(),
    fetch: fetchImpl,
    location: buttonLocation(),
  });
  await booted;
  await settle(20);

  assert.equal(cardOf(win).byTag("img").length, 0);
  assert.match(cardOf(win).textContent, /could not be loaded/);
});

// ── Validation happens before the network ─────────────────────────────────

test("an incomplete answer is refused locally, without a request", async () => {
  const context = await boot({ types: ["multiple_choice"] });
  const before = context.api.log.length;

  click(context.node("primary-action"));
  await settle(20);

  assert.equal(context.api.log.length, before, "an invalid answer was sent anyway");
  assert.equal(context.node("field-error").hidden, false);
  assert.match(context.node("field-error").textContent, /Choose one option/);
  assert.match(context.node("announcer").textContent, /Choose one option/);
});

test("the error clears once the card is answered", async () => {
  const context = await boot({ types: ["multiple_choice", "true_false"] });

  click(context.node("primary-action"));
  await settle(20);
  assert.equal(context.node("field-error").hidden, false);

  completeCard(cardOf(context.win), FIXTURES.multiple_choice.payload);
  click(context.node("primary-action"));
  await settle(20);

  assert.equal(context.node("field-error").hidden, true);
  assert.equal(context.node("field-error").textContent, "");
});

// ── Every failure has a state ─────────────────────────────────────────────

test("a launch outside Telegram says so and asks for nothing", async () => {
  const api = fakeApi();
  const { win, booted } = loadApp({
    telegram: null,
    fetch: api.fetchImpl,
    location: buttonLocation(),
  });
  await booted;
  await settle();

  assert.equal(stateOf(win), "unsupported");
  assert.match(cardOf(win).textContent, /Open this from Telegram/);
  assert.equal(api.log.length, 0, "a request was made without a launch context");
});

test("a Telegram launch with no initData is the same refusal", async () => {
  const api = fakeApi();
  const { win, booted } = loadApp({
    telegram: telegramStub({ initData: "" }),
    fetch: api.fetchImpl,
    location: buttonLocation(),
  });
  await booted;
  await settle();

  assert.equal(stateOf(win), "unsupported");
  assert.equal(api.log.length, 0);
});

test("an address with no launch selector ends in not-found, not a blank screen", async () => {
  const api = fakeApi();
  const { win, booted } = loadApp({
    telegram: telegramStub(),
    fetch: api.fetchImpl,
    location: { search: "", pathname: "/", hash: "" },
  });
  await booted;
  await settle();

  assert.equal(stateOf(win), "notfound");
  assert.equal(api.log.length, 0);
});

test("a malformed launch selector is not sent to the server", async () => {
  for (const hostile of [
    "#launch=../../etc/passwd",
    "#launch=short",
    "#launch=" + "x".repeat(200),
    "#launch=has spaces here at all",
    "#experience_id=exp-abc123",
    "#launch=",
  ]) {
    const api = fakeApi();
    const { win, booted } = loadApp({
      telegram: telegramStub(),
      fetch: api.fetchImpl,
      location: { search: "", pathname: "/", hash: hostile },
    });
    await booted;
    await settle();

    assert.equal(stateOf(win), "notfound", hostile);
    assert.equal(api.log.length, 0, hostile);
  }
});

test("the page never sends an experience id of its own choosing", async () => {
  // A query string naming an experience must not become launch authority: on a
  // launched runtime the server derives the exercise from the grant, and a
  // page that could name one would be back to the design this replaced.
  const api = fakeApi();
  const { win, booted } = loadApp({
    telegram: telegramStub(),
    fetch: api.fetchImpl,
    location: { search: "?experience_id=exp-abc123", pathname: "/", hash: "" },
  });
  await booted;
  await settle();

  assert.equal(stateOf(win), "notfound");
  assert.equal(api.log.length, 0);
});

test("the launch selector is removed from history as soon as it is read", async () => {
  const context = await boot({ types: ["multiple_choice"] });

  assert.deepEqual(context.win.history.replaced, ["/"]);
  assert.equal(JSON.parse(context.api.log[0].body).launch_id, LAUNCH_ID);
});

test("a reopen reuses the selector after the fragment has been stripped", async () => {
  // The page removes the fragment as soon as it reads it, so anything that
  // opens a session again -- an expired session, a retry -- must not depend on
  // it still being in the address bar.
  const context = await boot({
    types: ["multiple_choice"],
    // The next component request fails as an expired session, which is what
    // puts the reopen action on the screen.
    failWith: { "/api/session/component": 401 },
  });
  context.win.location.hash = "";

  click(context.node("primary-action"));
  await settle(20);
  click(context.node("primary-action"));
  await settle(20);

  const opens = context.api.log.filter((entry) => entry.path === "/api/session");
  assert.ok(opens.length >= 2, "the session was never reopened");
  assert.equal(JSON.parse(opens[opens.length - 1].body).launch_id, LAUNCH_ID);
});

const FAILURES = [
  { status: 401, path: "/api/session", state: "auth", action: null },
  { status: 403, path: "/api/session", state: "forbidden", action: null },
  { status: 404, path: "/api/session", state: "notfound", action: null },
  { status: 429, path: "/api/session", state: "throttled", action: "Try again" },
  { status: 500, path: "/api/session", state: "server", action: "Try again" },
  { status: "network", path: "/api/session", state: "offline", action: "Try again" },
];

for (const scenario of FAILURES) {
  test(`a ${scenario.status} on the bootstrap shows the ${scenario.state} state`, async () => {
    const context = await boot({
      types: ["true_false"],
      failWith: { [scenario.path]: scenario.status },
    });

    assert.equal(stateOf(context.win), scenario.state);
    assert.ok(cardOf(context.win).textContent.length > 0);
    if (scenario.action) {
      assert.match(context.node("primary-action").textContent, new RegExp(scenario.action));
    } else {
      assert.equal(context.node("primary-action").textContent, "");
      assert.equal(context.node("actions").hidden, true);
    }
  });
}

test("a session that expires mid-exercise offers to reopen", async () => {
  const context = await boot({ types: ["true_false", "short_answer"] });
  context.api.failWith["/api/session/answer"] = 401;

  completeCard(cardOf(context.win), FIXTURES.true_false.payload);
  click(context.node("primary-action"));
  await settle(20);

  assert.equal(stateOf(context.win), "expired");
  assert.match(cardOf(context.win).textContent, /session has ended/);
  assert.match(context.node("primary-action").textContent, /Reopen/);
});

test("reopening after an expiry starts a fresh session", async () => {
  const context = await boot({ types: ["true_false", "short_answer"] });
  context.api.failWith["/api/session/answer"] = 401;
  completeCard(cardOf(context.win), FIXTURES.true_false.payload);
  click(context.node("primary-action"));
  await settle(20);

  click(context.node("primary-action"));
  await settle(20);

  assert.equal(stateOf(context.win), null, "a card should be showing again");
  assert.equal(
    context.api.log.filter((entry) => entry.path === "/api/session").length,
    2,
    "reopening did not bootstrap a second session"
  );
});

test("a stale client answering the wrong card is told the card moved on", async () => {
  const context = await boot({ types: ["true_false", "short_answer"] });
  context.api.failWith["/api/session/answer"] = 409;

  completeCard(cardOf(context.win), FIXTURES.true_false.payload);
  click(context.node("primary-action"));
  await settle(20);

  assert.equal(stateOf(context.win), "conflict");
});

test("an unreachable server mid-exercise says offline, not something went wrong", async () => {
  const context = await boot({ types: ["true_false", "short_answer"] });
  context.api.failWith["/api/session/answer"] = "network";

  completeCard(cardOf(context.win), FIXTURES.true_false.payload);
  click(context.node("primary-action"));
  await settle(20);

  assert.equal(stateOf(context.win), "offline");
});

test("no state renders the server's own error text", async () => {
  // The fake answers every refusal with `{"error": "refused"}`. That string is
  // written for an operator, and the interface is localized; showing it would
  // undo both.
  const context = await boot({ types: ["true_false"], failWith: { "/api/session": 500 } });

  assert.ok(!cardOf(context.win).textContent.includes("refused"));
});

// ── Localization, theme, and progress ─────────────────────────────────────

test("the interface follows the experience's ui_locale", async () => {
  const context = await boot({ types: ["multi_select"], uiLocale: "fr" });

  assert.equal(context.win.document.documentElement.getAttribute("lang"), "fr");
  assert.match(context.node("primary-action").textContent, /Valider/);
  assert.match(context.node("progress-text").textContent, /Carte 1 sur 1/);
  assert.match(cardOf(context.win).textContent, /Choisissez/);
  assert.match(context.node("skip-link").textContent, /Aller à l'exercice/);
});

test("an unknown ui_locale falls back to English", async () => {
  const context = await boot({ types: ["true_false"], uiLocale: "kl-GL" });

  assert.equal(context.win.document.documentElement.getAttribute("lang"), "en");
  assert.match(context.node("primary-action").textContent, /Submit/);
});

test("Telegram theme parameters are applied as custom properties", async () => {
  const context = await boot({ types: ["true_false"] });
  const root = context.win.document.documentElement;

  assert.equal(root.getAttribute("data-theme"), "light");
  assert.equal(root.style.getPropertyValue("--tg-bg-color"), "#101010");
  assert.equal(root.style.getPropertyValue("--tg-text-color"), "#f0f0f0");
});

test("a dark colour scheme is honoured", async () => {
  const context = await boot({
    types: ["true_false"],
    telegram: { colorScheme: "dark" },
  });

  assert.equal(context.win.document.documentElement.getAttribute("data-theme"), "dark");
});

test("a theme value that is not a colour is ignored, not written into the page", async () => {
  const context = await boot({
    types: ["true_false"],
    telegram: {
      themeParams: {
        bg_color: "red; } * { display: none } :root {",
        text_color: "#abc",
      },
    },
  });
  const style = context.win.document.documentElement.style;

  assert.equal(style.getPropertyValue("--tg-bg-color"), "");
  assert.equal(style.getPropertyValue("--tg-text-color"), "#abc");
});

test("progress is reported as a value and as text", async () => {
  const context = await boot({ types: ["true_false", "short_answer", "multi_select"] });
  const bar = context.node("progress");

  assert.equal(bar.getAttribute("aria-valuemax"), "3");
  assert.equal(bar.getAttribute("aria-valuenow"), "0");
  assert.ok(bar.getAttribute("aria-label"));

  await answerCurrent(context);

  assert.equal(bar.getAttribute("aria-valuenow"), "1");
  assert.match(context.node("progress-text").textContent, /Card 2 of 3/);
  assert.equal(context.node("progress-fill").style.width, "33%");
});

// ── Nothing hidden, over a whole session ──────────────────────────────────

test("no canary appears anywhere in the page across a full walkthrough", async () => {
  const context = await boot({ types: ALL_TYPES });

  for (let index = 0; index < ALL_TYPES.length; index += 1) {
    const markup = context.win.document.documentElement.serialize();
    assert.ok(!markup.includes(CANARY), `a hidden field reached the page at card ${index}`);
    await answerCurrent(context);
  }

  assert.ok(!context.win.document.documentElement.serialize().includes(CANARY));
  assert.ok(!JSON.stringify(context.api.answers).includes(CANARY));
});

test("the session token never reaches the page or a url", async () => {
  const context = await boot({ types: ["true_false"] });

  assert.ok(!context.win.document.documentElement.serialize().includes("session-token-value"));
  for (const entry of context.api.log) {
    assert.ok(!entry.path.includes("session-token-value"));
  }
});

test("an unsupported card can be skipped so the exercise is not a dead end", async () => {
  const api = fakeApi({ types: ["true_false"] });
  api.components[0] = {
    position: 0,
    component_id: "component-0",
    type: "some_future_type",
    payload: { prompt: "Something new" },
  };
  const { win, booted } = loadApp({
    telegram: telegramStub(),
    fetch: api.fetchImpl,
    location: buttonLocation(),
  });
  await booted;
  await settle(20);

  assert.match(cardOf(win).textContent, /cannot be shown here/);
  assert.match(win.document.getElementById("primary-action").textContent, /Continue/);

  click(win.document.getElementById("primary-action"));
  await settle(20);

  assert.equal(api.answers.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(api.answers[0].response)), { skipped: true });
});

// ── Turning a flashcard over, through the real application ────────────────

test("a flashcard is turned over before it can be submitted", async () => {
  const context = await boot({ types: ["flashcard", "true_false"] });
  const card = cardOf(context.win);

  const turn = card.byTag("button").find((button) => button.className.includes("primary"));
  assert.ok(turn, "the card offers no way to turn it over");

  // Nothing has been asked for yet, and nothing has been shown.
  assert.equal(context.api.log.filter((e) => e.path === "/api/session/reveal").length, 0);
  assert.ok(!context.win.document.documentElement.serialize().includes(FLASHCARD_BACK));

  const area = card.byTag("textarea")[0];
  area.value = "my recall";
  click(turn);
  await settle(20);

  const reveal = context.api.log.find((entry) => entry.path === "/api/session/reveal");
  assert.ok(reveal, "no reveal request was made");
  assert.equal(JSON.parse(reveal.body).attempt, "my recall");
  assert.ok(cardOf(context.win).textContent.includes(FLASHCARD_BACK));
});

test("the answer is not in the page until the server sends it", async () => {
  const context = await boot({ types: ["flashcard"] });

  const before = context.win.document.documentElement.serialize();
  assert.ok(!before.includes(FLASHCARD_BACK));
  // And not in any response body received so far either.
  for (const entry of context.api.log) {
    assert.ok(!String(entry.body || "").includes(FLASHCARD_BACK));
  }
});

test("a flashcard reveal carries the session headers like every other request", async () => {
  const context = await boot({ types: ["flashcard"] });
  const card = cardOf(context.win);
  card.byTag("textarea")[0].value = "recall";
  click(card.byTag("button").find((b) => b.className.includes("primary")));
  await settle(20);

  const reveal = context.api.log.find((entry) => entry.path === "/api/session/reveal");

  assert.equal(reveal.headers[INIT_DATA_HEADER], INIT_DATA);
  assert.equal(reveal.headers[SESSION_HEADER], "session-token-value");
});

test("the recall the server froze is what gets submitted", async () => {
  const context = await boot({ types: ["flashcard", "true_false"] });
  const card = cardOf(context.win);
  card.byTag("textarea")[0].value = "my first answer";
  click(card.byTag("button").find((b) => b.className.includes("primary")));
  await settle(20);

  // Rate it and submit.
  const radios = cardOf(context.win)
    .byTag("input")
    .filter((input) => input.type === "radio");
  radios[1].checked = true;
  radios[1].dispatchEvent({ type: "change" });
  click(context.node("primary-action"));
  await settle(20);

  assert.equal(context.api.answers.length, 1);
  assert.equal(context.api.answers[0].response.text, "my first answer");
  assert.equal(context.api.reveals.get("component-0"), "my first answer");
});

test("a flashcard that was never turned over cannot be submitted", async () => {
  const context = await boot({ types: ["flashcard", "true_false"] });
  const radios = cardOf(context.win)
    .byTag("input")
    .filter((input) => input.type === "radio");
  radios[0].checked = true;
  radios[0].dispatchEvent({ type: "change" });

  click(context.node("primary-action"));
  await settle(20);

  assert.equal(context.api.answers.length, 0, "an unrevealed card was submitted");
  assert.match(context.node("field-error").textContent, /Turn the card over/);
});

// ── Document title and locale ─────────────────────────────────────────────

test("the document title is translated with the rest of the interface", async () => {
  const english = await boot({ types: ["true_false"] });
  assert.equal(english.win.document.title, "Learning Studio");

  const french = await boot({ types: ["true_false"], uiLocale: "fr" });
  assert.equal(french.win.document.title, "Studio d'apprentissage");
});

test("the card is marked with the exercise's language, the document with the reader's", async () => {
  const context = await boot({ types: ["true_false"], uiLocale: "fr", contentLocale: "ja" });

  assert.equal(context.win.document.documentElement.getAttribute("lang"), "fr");
  assert.equal(cardOf(context.win).children[0].getAttribute("lang"), "ja");
});

// ── Theme values ──────────────────────────────────────────────────────────

const THEME_CASES = [
  ["#abc", true],
  ["#abcd", true],
  ["#a1b2c3", true],
  ["#a1b2c3d4", true],
  ["#a", false],
  ["#ab", false],
  ["#abcde", false],
  ["#abcdefg", false],
  ["#abcdef123", false],
  ["#12345", false],
  ["#1234567", false],
  ["abcdef", false],
  ["red", false],
  ["var(--x)", false],
  ["#abc; } * { display: none", false],
  ["#abc\\3b  color: red", false],
  ["url(x)", false],
  ["", false],
];

for (const [value, accepted] of THEME_CASES) {
  test(`a theme colour of ${JSON.stringify(value)} is ${accepted ? "used" : "ignored"}`, async () => {
    const context = await boot({
      types: ["true_false"],
      telegram: { themeParams: { bg_color: value } },
    });
    const applied = context.win.document.documentElement.style.getPropertyValue("--tg-bg-color");

    assert.equal(applied, accepted ? value : "");
  });
}

test("one bad theme value does not discard the good ones beside it", async () => {
  const context = await boot({
    types: ["true_false"],
    telegram: { themeParams: { bg_color: "#12345", text_color: "#f0f0f0" } },
  });
  const style = context.win.document.documentElement.style;

  assert.equal(style.getPropertyValue("--tg-bg-color"), "");
  assert.equal(style.getPropertyValue("--tg-text-color"), "#f0f0f0");
});

// ── Aggregate request size ────────────────────────────────────────────────

/**
 * A card with the most prompts the registry allows.
 *
 * The aggregate check only matters where it can actually be reached: each field
 * is capped at 4,000 characters by its own `maxlength`, so two fields can never
 * exceed a 16 KB body. Eight of them can, which is precisely the case a
 * per-field limit does not see.
 */
function eightPromptCard() {
  const api = fakeApi({ types: ["reflection"] });
  api.components[0] = {
    position: 0,
    component_id: "component-0",
    type: "reflection",
    payload: {
      prompt: "Look back over this session.",
      content: {
        prompts: Array.from({ length: 8 }, (_index, n) => `Prompt ${n + 1}?`),
        min_words: 40,
      },
    },
  };
  return api;
}

async function bootWith(api) {
  const { win, booted } = loadApp({
    telegram: telegramStub(),
    fetch: api.fetchImpl,
    location: buttonLocation(),
  });
  await booted;
  await settle(20);
  return { win, api, node: (id) => win.document.getElementById(id) };
}

test("an over-large answer is refused locally with a readable reason", async () => {
  const context = await bootWith(eightPromptCard());
  const before = context.api.log.length;

  // Every field is inside the per-string limit; together they are not.
  for (const area of cardOf(context.win).byTag("textarea")) {
    area.value = "word ".repeat(800).trim();
    area.dispatchEvent({ type: "input" });
  }
  click(context.node("primary-action"));
  await settle(20);

  assert.equal(context.api.log.length, before, "an oversized body was sent anyway");
  assert.match(context.node("field-error").textContent, /too long to send/);
});

test("an answer within the ceiling is sent", async () => {
  const context = await bootWith(eightPromptCard());

  for (const area of cardOf(context.win).byTag("textarea")) {
    area.value = "word ".repeat(20).trim();
    area.dispatchEvent({ type: "input" });
  }
  click(context.node("primary-action"));
  await settle(20);

  assert.equal(context.api.answers.length, 1);
});

test("the size ceiling is counted in bytes, not characters", async () => {
  const context = await bootWith(eightPromptCard());

  // Three bytes per character: well inside every character limit, well over the
  // byte ceiling. A `length` check would have let this through.
  for (const area of cardOf(context.win).byTag("textarea")) {
    area.value = "\u3042 ".repeat(1500).trim();
    area.dispatchEvent({ type: "input" });
  }
  click(context.node("primary-action"));
  await settle(20);

  assert.equal(context.api.answers.length, 0, "a body over the byte ceiling was sent");
  assert.match(context.node("field-error").textContent, /too long to send/);
});

// ── A theme update replaces the palette; it does not add to it ────────────

/** Apply a new palette the way Telegram's `themeChanged` event does. */
async function changeTheme(context, params, colorScheme) {
  context.telegram.themeParams = params;
  if (colorScheme) {
    context.telegram.colorScheme = colorScheme;
  }
  // `app.js` subscribed to this event at boot; fire what it subscribed with.
  context.telegram.calls.handlers.themeChanged();
  await settle(5);
}

function themeProperty(context, name) {
  return context.win.document.documentElement.style.getPropertyValue(name);
}

test("a theme value that disappears from a later palette is removed", async () => {
  const context = await boot({
    types: ["true_false"],
    telegram: { themeParams: { bg_color: "#101010", text_color: "#f0f0f0" } },
  });
  assert.equal(themeProperty(context, "--tg-bg-color"), "#101010");

  await changeTheme(context, { text_color: "#f0f0f0" });

  assert.equal(
    themeProperty(context, "--tg-bg-color"),
    "",
    "a stale background survived a palette that no longer declares one"
  );
  assert.equal(themeProperty(context, "--tg-text-color"), "#f0f0f0");
});

test("a theme value that becomes invalid is removed rather than kept", async () => {
  const context = await boot({
    types: ["true_false"],
    telegram: { themeParams: { bg_color: "#101010" } },
  });

  await changeTheme(context, { bg_color: "#12345" });

  assert.equal(themeProperty(context, "--tg-bg-color"), "");
});

test("switching from light to dark does not leave the light palette behind", async () => {
  // The case that matters visually: light background under dark text.
  const context = await boot({
    types: ["true_false"],
    telegram: {
      colorScheme: "light",
      themeParams: { bg_color: "#ffffff", text_color: "#000000" },
    },
  });

  await changeTheme(context, { bg_color: "#17212b", text_color: "#f2f5f8" }, "dark");

  assert.equal(context.win.document.documentElement.getAttribute("data-theme"), "dark");
  assert.equal(themeProperty(context, "--tg-bg-color"), "#17212b");
  assert.equal(themeProperty(context, "--tg-text-color"), "#f2f5f8");
});

test("a partial palette leaves the rest to the stylesheet", async () => {
  const context = await boot({
    types: ["true_false"],
    telegram: {
      themeParams: {
        bg_color: "#101010",
        text_color: "#f0f0f0",
        button_color: "#2470c0",
        hint_color: "#9aa8b6",
      },
    },
  });

  await changeTheme(context, { button_color: "#2470c0" }, "dark");

  assert.equal(themeProperty(context, "--tg-button-color"), "#2470c0");
  for (const absent of ["--tg-bg-color", "--tg-text-color", "--tg-hint-color"]) {
    assert.equal(themeProperty(context, absent), "", `${absent} was left stale`);
  }
});

test("an empty palette clears every property it previously set", async () => {
  const context = await boot({
    types: ["true_false"],
    telegram: {
      themeParams: {
        bg_color: "#101010",
        text_color: "#f0f0f0",
        hint_color: "#9aa8b6",
        link_color: "#78b6ff",
        button_color: "#2470c0",
        button_text_color: "#ffffff",
        secondary_bg_color: "#22303d",
      },
    },
  });

  await changeTheme(context, {});

  for (const property of [
    "--tg-bg-color",
    "--tg-text-color",
    "--tg-hint-color",
    "--tg-link-color",
    "--tg-button-color",
    "--tg-button-text-color",
    "--tg-secondary-bg-color",
  ]) {
    assert.equal(themeProperty(context, property), "", `${property} survived an empty palette`);
  }
});
