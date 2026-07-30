/*
 * Filling in a card, by the shape of its controls rather than by its type.
 *
 * Deliberately generic. A per-type completion table would be a second
 * implementation of every renderer, agreeing with the first by construction and
 * therefore proving nothing; a filler that only knows "a radio group needs one
 * of its buttons pressed" is an independent statement about the card, and it is
 * what lets the flow test walk all thirty-one types without knowing what any of
 * them mean.
 */

import { check, click, press, type } from "./dom.mjs";

/** Enough words to satisfy a declared minimum without breaking a maximum. */
function filler(content) {
  const minimum = Number(content.min_words || 0);
  const maximum = Number(content.max_words || 0);
  let count = Math.max(minimum, 1);
  if (maximum && count > maximum) {
    count = maximum;
  }
  return new Array(count).fill("word").join(" ");
}

/**
 * Complete every control in a rendered card.
 *
 * Returns what was touched, so a test can assert that a card actually offered
 * something to answer rather than quietly rendering nothing.
 */
export function completeCard(card, payload = {}) {
  const content = payload.content || {};
  const text = filler(content);
  const touched = {
    radios: 0,
    checkboxes: 0,
    texts: 0,
    selects: 0,
    hotspots: 0,
    orderings: 0,
    reveals: 0,
  };

  const groups = new Map();
  for (const input of card.byTag("input")) {
    if (input.type === "radio" || input.type === "checkbox") {
      const key = `${input.type}:${input.name}`;
      if (!groups.has(key)) {
        groups.set(key, []);
      }
      groups.get(key).push(input);
      continue;
    }
    type(input, text);
    touched.texts += 1;
  }

  for (const [key, inputs] of groups) {
    // One press per group: a radio group needs exactly one, and a checkbox
    // group needs at least one.
    check(inputs[0]);
    touched[key.startsWith("radio") ? "radios" : "checkboxes"] += 1;
  }

  for (const area of card.byTag("textarea")) {
    type(area, text);
    touched.texts += 1;
  }

  for (const select of card.byTag("select")) {
    const options = select.children.filter((option) => option.value);
    if (options.length) {
      select.value = options[0].value;
      touched.selects += 1;
    }
  }

  for (const surface of card.byTag("button")) {
    if (surface.className.includes("hotspot")) {
      press(surface, "ArrowRight");
      touched.hotspots += 1;
    }
  }

  // A flashcard has to be turned over before it can be rated, and turning it
  // over is a request. The click is fired here; the caller has to `await settle()`
  // before reading, because the back arrives asynchronously — which is exactly
  // what happens on a phone.
  for (const button of card.byTag("button")) {
    if (button.className.includes("primary") && !button.hidden) {
      click(button);
      touched.reveals += 1;
    }
  }

  // An ordering card arrives with a complete-but-arbitrary order, so there is
  // nothing to fill in. Moving the first row down is what proves the buttons are
  // wired, and it makes the submitted order something other than the given one.
  for (const ordered of card.byTag("ol")) {
    if (!ordered.className.includes("ordered")) {
      continue;
    }
    const rows = ordered.byTag("li");
    if (rows.length > 1) {
      const down = rows[0].byTag("button")[1];
      if (down && !down.disabled) {
        click(down);
      }
    }
    touched.orderings += 1;
  }

  return touched;
}
