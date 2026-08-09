/*
 * The card icons and the score ring, executed.
 *
 * The mapping below is written out by hand rather than read from `icons.js`,
 * which is the whole point of it being here: a test that asked the module which
 * icon it uses for `timeline` and then asserted it uses that one would agree
 * with any answer. This table is an independent statement of what a learner
 * should see, so a type quietly moving to a different family fails.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { loadRenderers, payloads, renderContext, settle } from "./harness.mjs";

const FIXTURES = payloads();
const TYPES = Object.keys(FIXTURES).sort();

/** Every registry type, and the icon family it belongs to. */
const ICON_FOR_TYPE = {
  multiple_choice: "choice",
  image_choice: "choice",
  scenario_choice: "choice",

  true_false: "truefalse",

  short_answer: "text",
  translation: "text",
  typed_recall: "text",
  error_correction: "text",

  diagram: "diagram",

  fill_blank: "cloze",

  code_response: "code",

  multi_select: "set",
  classification: "set",
  categorization: "set",
  matching: "set",
  labeling: "set",

  sentence_order: "order",
  sequence_order: "order",
  timeline: "order",
  process_flow: "order",
  decision_path: "order",

  table_grid: "grid",

  hotspot: "hotspot",

  flashcard: "flashcard",

  free_response: "rubric",
  image_observation: "rubric",
  case_study: "rubric",
  self_explanation: "rubric",
  rubric_response: "rubric",

  confidence_rating: "scale",

  reflection: "reflect",
};

function fresh(options) {
  const win = loadRenderers();
  return { win, api: win.LearningStudioRenderers, icons: win.LearningStudioIcons, ctx: renderContext(win, options) };
}

function renderType(componentType, options) {
  const context = fresh(options);
  return Object.assign(context, {
    component: FIXTURES[componentType],
    card: context.api.render(FIXTURES[componentType], context.ctx),
  });
}

function badgeOf(card) {
  return card.element.all().find((node) => node.className === "type-badge");
}

// ── One icon per card, for every type in the registry ─────────────────────

test("the icon table covers exactly the types the registry can produce", () => {
  const { icons } = fresh();

  assert.deepEqual(Object.keys(icons.TYPE_ICONS).sort(), TYPES);
  assert.deepEqual(Object.keys(ICON_FOR_TYPE).sort(), TYPES);
});

test("every icon a type names is actually drawn somewhere in the set", () => {
  const { icons } = fresh();

  for (const [componentType, name] of Object.entries(icons.TYPE_ICONS)) {
    assert.ok(
      Object.prototype.hasOwnProperty.call(icons.ICONS, name),
      `${componentType} names the icon "${name}", which does not exist`
    );
    assert.ok(icons.ICONS[name].length > 0, `the "${name}" icon draws nothing`);
  }
});

test("every card shows the icon for its own kind of card", async () => {
  for (const componentType of TYPES) {
    const { card } = renderType(componentType);
    await settle();

    const badge = badgeOf(card);
    assert.ok(badge, `${componentType}: no type badge`);

    const drawing = badge.children.find((node) => node.tagName === "svg");
    assert.ok(drawing, `${componentType}: the badge carries no icon`);
    assert.equal(
      drawing.getAttribute("data-icon"),
      ICON_FOR_TYPE[componentType],
      `${componentType} is drawn with the wrong icon`
    );
  }
});

test("the badge names the kind of card in the reader's own language", () => {
  const english = renderType("multiple_choice", { locale: "en" });
  const french = renderType("multiple_choice", { locale: "fr" });
  const spanish = renderType("multiple_choice", { locale: "es" });

  assert.match(badgeOf(english.card).textContent, /Multiple choice/);
  assert.match(badgeOf(french.card).textContent, /Choix multiple/);
  assert.match(badgeOf(spanish.card).textContent, /Opción múltiple/);
});

test("a type this build has never heard of gets the generic card, not a hole", () => {
  const { api, ctx } = fresh();

  const card = api.render(
    { component_id: "x", type: "holographic_interpretive_dance", payload: { prompt: "Do it" } },
    ctx
  );
  const badge = badgeOf(card);

  assert.ok(badge, "an unsupported card lost its badge");
  assert.equal(badge.children[0].getAttribute("data-icon"), "card");
  // There is no name for it, so nothing is written — least of all the key.
  assert.equal(badge.textContent, "");
  assert.ok(!badge.textContent.includes("card.type"));
});

test("an icon is inert decoration, drawn with presentation attributes only", async () => {
  for (const componentType of TYPES) {
    const { card } = renderType(componentType);
    await settle();
    const drawing = badgeOf(card).children.find((node) => node.tagName === "svg");

    assert.equal(drawing.getAttribute("aria-hidden"), "true", componentType);
    assert.equal(drawing.getAttribute("viewBox"), "0 0 24 24", componentType);
    assert.equal(drawing.getAttribute("fill"), "none", componentType);
    assert.equal(drawing.getAttribute("stroke"), "currentColor", componentType);
    assert.equal(drawing.getAttribute("stroke-width"), "1.8", componentType);
    assert.equal(drawing.getAttribute("stroke-linecap"), "round", componentType);
    assert.equal(drawing.getAttribute("stroke-linejoin"), "round", componentType);

    for (const node of drawing.all()) {
      assert.equal(node.hasAttribute("style"), false, `${componentType}: an icon carries a style`);
      assert.equal(node.namespaceURI, "http://www.w3.org/2000/svg", componentType);
    }
  }
});

test("the badge is the first thing in the card, ahead of the question", async () => {
  for (const componentType of TYPES) {
    const { card } = renderType(componentType);
    await settle();

    assert.equal(card.element.children[0].className, "type-badge", componentType);
    assert.equal(card.element.children[1].className, "prompt", componentType);
  }
});

test("no icon carries exercise text, an identifier, or a colour of its own", async () => {
  for (const componentType of TYPES) {
    const { card, component } = renderType(componentType);
    await settle();
    const markup = badgeOf(card)
      .children.find((node) => node.tagName === "svg")
      .serialize();

    assert.ok(!markup.includes(component.component_id), componentType);
    assert.ok(!/#[0-9a-fA-F]{3}/.test(markup), `${componentType}: an icon hardcodes a colour`);
  }
});

// ── The score ring ────────────────────────────────────────────────────────

function ringFor(icons, correct, graded, options = {}) {
  return icons.ring(
    Object.assign({ correct, graded, tone: "ok", label: `${correct} of ${graded}` }, options)
  );
}

/** One part of a ring, by the class the stylesheet colours it with. */
function part(node, name) {
  return node.all().find((child) => child.getAttribute("class") === name);
}

test("the ring's arc is the fraction it is given", () => {
  const { icons } = fresh();

  const quarter = ringFor(icons, 1, 4);
  const full = ringFor(icons, 4, 4);
  const empty = ringFor(icons, 0, 4);

  const length = Number(part(quarter, "ring-arc").getAttribute("stroke-dasharray"));
  const offsetOf = (node) => Number(part(node, "ring-arc").getAttribute("stroke-dashoffset"));

  assert.ok(length > 0);
  assert.notEqual(offsetOf(quarter), offsetOf(full), "every score drew the same ring");
  assert.equal(offsetOf(full), 0, "a perfect score does not close the ring");
  assert.equal(offsetOf(empty).toFixed(2), length.toFixed(2), "nothing right still drew an arc");
  assert.ok(
    Math.abs(offsetOf(quarter) - length * 0.75) < 0.02,
    "one of four is not three quarters of the way round"
  );
});

test("the ring is decoration, because the sentence beside it is the real text", () => {
  const { icons } = fresh();
  // A label is still offered, to prove the ring does not put one in the page
  // merely because a caller passed one.
  const node = ringFor(icons, 3, 4, { label: "You got 3 of 4 marked answers right." });

  // `app.js` pushes the localized score paragraph immediately after this node,
  // and that paragraph is the single accessible text equivalent for the mark.
  // A ring that named itself as well would make linear screen-reader navigation
  // announce the same score twice, once as an image and once as prose.
  assert.equal(node.getAttribute("aria-hidden"), "true");
  assert.equal(node.hasAttribute("role"), false, "the ring still claims to be an image");
  assert.equal(node.hasAttribute("aria-label"), false, "the ring still names itself");
  // The stylesheet still colours it, so the class it is found by does not move.
  assert.equal(node.getAttribute("class"), "score-ring");

  // The digits stay visible — they are the picture — and stay hidden from
  // assistive technology in their own right, not only by inheritance.
  const value = part(node, "ring-value");
  assert.equal(value.textContent, "3/4");
  assert.equal(value.getAttribute("aria-hidden"), "true");
});

test("the ring is coloured by a class, so the palette stays in the stylesheet", () => {
  const { icons } = fresh();

  for (const tone of ["ok", "accent", "danger"]) {
    const node = ringFor(icons, 1, 2, { tone });

    assert.equal(node.getAttribute("data-tone"), tone);
    for (const child of node.all()) {
      assert.equal(child.hasAttribute("stroke"), false, "the ring hardcodes a colour");
      assert.equal(child.hasAttribute("fill") && child.getAttribute("fill") !== "none", false);
    }
  }
});

test("a ring with nothing marked is empty rather than undefined", () => {
  const { icons } = fresh();
  const node = ringFor(icons, 0, 0, { label: "" });

  assert.equal(part(node, "ring-value").textContent, "0/0");
  assert.ok(Number(part(node, "ring-arc").getAttribute("stroke-dashoffset")) > 0);
});

test("the ring starts at the top, not at three o'clock", () => {
  const { icons } = fresh();

  assert.match(part(ringFor(icons, 1, 2), "ring-arc").getAttribute("transform"), /^rotate\(-90 /);
});
