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

const INIT_DATA_HEADER = "X-Telegram-Init-Data";
const SESSION_HEADER = "X-Learning-Studio-Session";

const EXPERIENCE_ID = "exp-abc123";
const INIT_DATA = `auth_date=1800000000&start_param=${EXPERIENCE_ID}&hash=deadbeef`;

function telegramStub(overrides = {}) {
  const calls = { ready: 0, expand: 0, close: 0, closingConfirmation: 0, events: [] };
  return Object.assign(
    {
      initData: INIT_DATA,
      initDataUnsafe: { start_param: EXPERIENCE_ID },
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
      onEvent(name) {
        calls.events.push(name);
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
function fakeApi({ types = ["multiple_choice"], uiLocale = "en", failWith = {} } = {}) {
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
    content_locale: "en",
    expected_duration_minutes: 12,
    difficulty: "intermediate",
    accessibility: {},
    component_count: components.length,
  };

  const log = [];
  const answers = [];
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
      });
    }
    if (path === "/api/session/component") {
      return jsonResponse(200, { progress: progress(), component: componentAt(position) });
    }
    if (path === "/api/session/answer") {
      const sent = JSON.parse(init.body);
      const current = componentAt(position);
      if (!current || sent.component_id !== current.component_id) {
        return jsonResponse(409, { error: "not the current question" });
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

  return { fetchImpl, log, answers, components, experience, progress, failWith };
}

async function boot(options = {}) {
  const api = fakeApi(options);
  const telegram = options.telegram === null ? null : telegramStub(options.telegram);
  const { win, booted } = loadApp({ telegram, fetch: api.fetchImpl });
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
  assert.equal(JSON.parse(context.api.log[0].body).experience_id, EXPERIENCE_ID);
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
  assert.match(cardOf(context.win).textContent, /Nothing has been marked/);
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
  assert.match(cardOf(context.win).textContent, /does not score answers yet/);
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
  const { win, booted } = loadApp({ telegram: telegramStub(), fetch: fetchImpl });
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
  const { win, booted } = loadApp({ telegram: null, fetch: api.fetchImpl });
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
  });
  await booted;
  await settle();

  assert.equal(stateOf(win), "unsupported");
  assert.equal(api.log.length, 0);
});

test("a deep link with no experience id ends in not-found, not a blank screen", async () => {
  const api = fakeApi();
  const { win, booted } = loadApp({
    telegram: telegramStub({ initData: "auth_date=1&hash=x", initDataUnsafe: {} }),
    fetch: api.fetchImpl,
  });
  await booted;
  await settle();

  assert.equal(stateOf(win), "notfound");
  assert.equal(api.log.length, 0);
});

test("a malformed experience id is not sent to the server", async () => {
  const api = fakeApi();
  const { win, booted } = loadApp({
    telegram: telegramStub({
      initData: "auth_date=1&start_param=../../etc/passwd&hash=x",
      initDataUnsafe: { start_param: "../../etc/passwd" },
    }),
    fetch: api.fetchImpl,
  });
  await booted;
  await settle();

  assert.equal(stateOf(win), "notfound");
  assert.equal(api.log.length, 0);
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
  const { win, booted } = loadApp({ telegram: telegramStub(), fetch: api.fetchImpl });
  await booted;
  await settle(20);

  assert.match(cardOf(win).textContent, /cannot be shown here/);
  assert.match(win.document.getElementById("primary-action").textContent, /Continue/);

  click(win.document.getElementById("primary-action"));
  await settle(20);

  assert.equal(api.answers.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(api.answers[0].response)), { skipped: true });
});
