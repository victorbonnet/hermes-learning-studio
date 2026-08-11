/*
 * The card icon set, and the completion screen's score ring.
 *
 * Why this is a file of its own rather than a few more functions in
 * `renderers.js`:
 *
 * 1. **SVG is built through a different door.** An `<svg>` and everything
 *    inside it belongs to the SVG namespace, so it is created with
 *    `createElementNS` rather than `createElement`. Keeping the two apart means
 *    `renderers.js` still has exactly one way for an element to exist -- a
 *    contract test counts the call sites -- and this file has exactly one too.
 *
 * 2. **The drawings are data.** Every icon below is a list of shapes and
 *    numbers. Nothing here reads a payload, and no string from the server ever
 *    reaches this file: an icon is chosen by *component type*, which is the
 *    application's own discriminator, and an unknown type gets the generic card
 *    rather than an error. There is no `innerHTML` here for the same reason
 *    there is none in `renderers.js`, and the same test greps for it.
 *
 * 3. **Colour is not decided here.** Icons are stroked with `currentColor` and
 *    the ring's arc is coloured by a class in `app.css`, so both follow the
 *    Telegram theme and the contrast-tested palette. This file writes no
 *    colour value at all.
 *
 * The one URL in this file is the SVG namespace. It is an XML identifier, not
 * an address -- nothing fetches it, and a document may not contain SVG without
 * naming it.
 */

(function (global) {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";

  //: The look of every icon, applied to the root so no shape has to repeat it.
  //: Presentation attributes rather than a `style` attribute: the document's
  //: Content-Security-Policy has no `'unsafe-inline'`, and these are attributes
  //: a stylesheet can still override.
  var ICON_ROOT = {
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "1.8",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    // Every icon sits beside a text label or inside a control that already has
    // an accessible name, so it is decoration to a screen reader.
    "aria-hidden": "true",
  };

  //: A filled dot: the one place a shape overrides the root's `fill: none`.
  var DOT = { fill: "currentColor", stroke: "none" };

  /**
   * The drawings, one per family rather than one per component type.
   *
   * Thirty-one different pictures would be thirty-one things to recognise; the
   * families below are what a learner actually needs to tell apart at a glance
   * -- "this is a choice", "this is something to write", "this is something to
   * put in order".
   */
  var ICONS = {
    // Selection: a radio pair, one of them chosen.
    choice: [
      ["circle", { cx: "6", cy: "8", r: "3" }],
      ["circle", { cx: "6", cy: "8", r: "1.4", fill: DOT.fill, stroke: DOT.stroke }],
      ["circle", { cx: "6", cy: "17", r: "3" }],
      ["line", { x1: "12", y1: "8", x2: "20", y2: "8" }],
      ["line", { x1: "12", y1: "17", x2: "20", y2: "17" }],
    ],
    // True or false: a tick and a cross, no hint as to which one applies.
    truefalse: [
      ["path", { d: "M3 12.5l2.6 2.6L11 9.7" }],
      ["line", { x1: "15", y1: "9", x2: "21", y2: "15" }],
      ["line", { x1: "21", y1: "9", x2: "15", y2: "15" }],
    ],
    // Something to write: a pencil.
    text: [
      ["path", { d: "M4.5 19.5h3.6L20 8.1a2.3 2.3 0 0 0-3.3-3.3L5.2 16.4z" }],
      ["line", { x1: "14.8", y1: "6.2", x2: "18.1", y2: "9.5" }],
    ],
    // A picture with something named in it.
    diagram: [
      ["rect", { x: "3", y: "4.5", width: "18", height: "15", rx: "2" }],
      ["circle", { cx: "8", cy: "10", r: "2.2" }],
      ["line", { x1: "13", y1: "10", x2: "18", y2: "10" }],
      ["line", { x1: "6", y1: "15.5", x2: "18", y2: "15.5" }],
    ],
    // A gap in a sentence: a line of prose over three blanks.
    cloze: [
      ["line", { x1: "3", y1: "8", x2: "21", y2: "8" }],
      ["line", { x1: "3", y1: "15", x2: "8", y2: "15" }],
      ["line", { x1: "11", y1: "15", x2: "14", y2: "15" }],
      ["line", { x1: "17", y1: "15", x2: "21", y2: "15" }],
    ],
    // Code, which this application only ever stores as text.
    code: [
      ["path", { d: "M8.5 8.5L4.5 12l4 3.5" }],
      ["path", { d: "M15.5 8.5l4 3.5-4 3.5" }],
      ["line", { x1: "13.4", y1: "5.5", x2: "10.6", y2: "18.5" }],
    ],
    // A set of things to sort, match, or label: a grid of tiles.
    set: [
      ["rect", { x: "3.5", y: "3.5", width: "7", height: "7", rx: "1.6" }],
      ["rect", { x: "13.5", y: "3.5", width: "7", height: "7", rx: "1.6" }],
      ["rect", { x: "3.5", y: "13.5", width: "7", height: "7", rx: "1.6" }],
      ["rect", { x: "13.5", y: "13.5", width: "7", height: "7", rx: "1.6" }],
    ],
    // An order to establish: a move-down arrow beside two rows.
    order: [
      ["line", { x1: "6", y1: "4.5", x2: "6", y2: "18" }],
      ["path", { d: "M3 15l3 3.5 3-3.5" }],
      ["line", { x1: "12", y1: "8", x2: "21", y2: "8" }],
      ["line", { x1: "12", y1: "15", x2: "21", y2: "15" }],
    ],
    // A table to complete.
    grid: [
      ["rect", { x: "3", y: "5", width: "18", height: "14", rx: "2" }],
      ["line", { x1: "3", y1: "10", x2: "21", y2: "10" }],
      ["line", { x1: "9.5", y1: "10", x2: "9.5", y2: "19" }],
    ],
    // A point to choose on a picture: a crosshair.
    hotspot: [
      ["circle", { cx: "12", cy: "12", r: "5.5" }],
      ["circle", { cx: "12", cy: "12", r: "1.3", fill: DOT.fill, stroke: DOT.stroke }],
      ["line", { x1: "12", y1: "2.5", x2: "12", y2: "5" }],
      ["line", { x1: "12", y1: "19", x2: "12", y2: "21.5" }],
      ["line", { x1: "2.5", y1: "12", x2: "5", y2: "12" }],
      ["line", { x1: "19", y1: "12", x2: "21.5", y2: "12" }],
    ],
    // A card with a front and a back, one behind the other.
    flashcard: [
      ["rect", { x: "7.5", y: "3.5", width: "13", height: "13", rx: "2" }],
      ["path", { d: "M16.5 16.5v2a2 2 0 0 1-2 2h-9a2 2 0 0 1-2-2v-9a2 2 0 0 1 2-2h2" }],
    ],
    // Something to say in your own words.
    rubric: [
      [
        "path",
        {
          d:
            "M20.5 12.5a6.5 6.5 0 0 1-6.5 6.5H9.5l-5 2.5 1.4-3.7" +
            "A6.5 6.5 0 0 1 9.5 5.5H14a6.5 6.5 0 0 1 6.5 6.5z",
        },
      ],
      ["line", { x1: "9", y1: "10.5", x2: "16", y2: "10.5" }],
      ["line", { x1: "9", y1: "14", x2: "13.5", y2: "14" }],
    ],
    // A judgement about yourself, on a scale.
    scale: [
      ["path", { d: "M12 3.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8-4.3-4.1 5.9-.9z" }],
    ],
    // Looking back at your own thinking.
    reflect: [
      ["path", { d: "M10 3.5l1.5 3.8 3.8 1.5-3.8 1.5L10 14.1 8.5 10.3 4.7 8.8l3.8-1.5z" }],
      ["path", { d: "M17.5 13.5l.9 2.3 2.3.9-2.3.9-.9 2.3-.9-2.3-2.3-.9 2.3-.9z" }],
    ],
    // The generic card, for a type this build has never heard of.
    card: [
      ["rect", { x: "3", y: "4.5", width: "18", height: "15", rx: "2" }],
      ["line", { x1: "7", y1: "9.5", x2: "17", y2: "9.5" }],
      ["line", { x1: "7", y1: "14.5", x2: "13", y2: "14.5" }],
    ],
  };

  /**
   * Which family each component type belongs to.
   *
   * Every type in the registry is named here, and a test compares this table
   * with the registry itself: a new component type that nobody gave an icon
   * would otherwise render a card that looks like nothing in particular.
   */
  var TYPE_ICONS = {
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

  //: What an unknown type gets. The frontend can legitimately be behind a
  //: server that knows a newer registry, and a missing icon is not a reason to
  //: show a card with a hole in it.
  var FALLBACK_ICON = "card";

  function doc() {
    return global.document;
  }

  /** One SVG element, with its attributes. The only DOM constructor here. */
  function shape(tag, attrs) {
    var node = doc().createElementNS(SVG_NS, tag);
    if (attrs) {
      for (var key in attrs) {
        if (Object.prototype.hasOwnProperty.call(attrs, key)) {
          node.setAttribute(key, String(attrs[key]));
        }
      }
    }
    return node;
  }

  /** An `<svg>` root: the shared look, plus whatever this drawing adds. */
  function svgRoot(base, extra) {
    var attrs = {};
    var key;
    for (key in base) {
      if (Object.prototype.hasOwnProperty.call(base, key)) {
        attrs[key] = base[key];
      }
    }
    for (key in extra) {
      if (Object.prototype.hasOwnProperty.call(extra, key)) {
        attrs[key] = extra[key];
      }
    }
    return shape("svg", attrs);
  }

  /** The family an icon for this component type comes from. */
  function iconFor(componentType) {
    return Object.prototype.hasOwnProperty.call(TYPE_ICONS, componentType)
      ? TYPE_ICONS[componentType]
      : FALLBACK_ICON;
  }

  /**
   * One icon, as an inert `<svg>`.
   *
   * `null` for a name that is not in the set, so a caller cannot end up
   * appending an empty square.
   */
  function icon(name) {
    if (!Object.prototype.hasOwnProperty.call(ICONS, name)) {
      return null;
    }
    var node = svgRoot(ICON_ROOT, { class: "icon", "data-icon": name });
    ICONS[name].forEach(function (entry) {
      node.appendChild(shape(entry[0], entry[1]));
    });
    return node;
  }

  // ── The score ring ────────────────────────────────────────────────────

  //: The ring's geometry, in its own user units. The element is sized in CSS,
  //: so these numbers only decide the proportions.
  var RING_SIZE = 96;
  var RING_STROKE = 9;
  var RING_RADIUS = (RING_SIZE - RING_STROKE) / 2;
  var RING_CENTRE = RING_SIZE / 2;
  var RING_LENGTH = 2 * Math.PI * RING_RADIUS;

  //: Enough decimals that the arc lands where the arithmetic says, few enough
  //: that the attribute stays readable in a diff.
  var RING_PRECISION = 2;

  /**
   * The completion screen's mark, drawn as a ring.
   *
   * It shows the *same* fraction the sentence underneath it states -- correct
   * marked answers out of marked answers -- and it is a picture of that
   * sentence rather than a second source of truth. So it is decoration:
   * `aria-hidden`, with no role and no name. `app.js` pushes the localized
   * score paragraph immediately after it, and that paragraph is the single
   * accessible text equivalent for the mark. Naming the ring as well made a
   * screen reader read the same score twice in linear navigation, once as an
   * image and once as the prose beneath it.
   *
   * There is no animation. A ring that swept round would be a moving thing on
   * the one screen a learner reads carefully, and honouring reduced motion for
   * it would mean building the same drawing twice.
   */
  function ring(options) {
    var settings = options || {};
    var graded = Math.max(0, Number(settings.graded) || 0);
    var correct = Math.min(graded, Math.max(0, Number(settings.correct) || 0));
    var fraction = graded > 0 ? correct / graded : 0;
    var offset = RING_LENGTH * (1 - fraction);

    var node = svgRoot(
      { viewBox: "0 0 " + RING_SIZE + " " + RING_SIZE },
      {
        class: "score-ring",
        "data-tone": settings.tone || "accent",
        // Decoration. The visible sentence below is the same claim in words,
        // and it is the one an assistive technology should read.
        "aria-hidden": "true",
      }
    );

    node.appendChild(
      shape("circle", {
        class: "ring-track",
        cx: RING_CENTRE,
        cy: RING_CENTRE,
        r: RING_RADIUS,
        fill: "none",
        "stroke-width": RING_STROKE,
      })
    );
    node.appendChild(
      shape("circle", {
        class: "ring-arc",
        cx: RING_CENTRE,
        cy: RING_CENTRE,
        r: RING_RADIUS,
        fill: "none",
        "stroke-width": RING_STROKE,
        "stroke-linecap": "round",
        "stroke-dasharray": RING_LENGTH.toFixed(RING_PRECISION),
        "stroke-dashoffset": offset.toFixed(RING_PRECISION),
        // Twelve o'clock rather than three: a mark that started on the right
        // reads as a different quantity than the one it is.
        transform: "rotate(-90 " + RING_CENTRE + " " + RING_CENTRE + ")",
      })
    );

    var value = shape("text", {
      class: "ring-value",
      x: RING_CENTRE,
      y: RING_CENTRE,
      "text-anchor": "middle",
      "dominant-baseline": "central",
      // Digits and a solidus: the words are in the translated sentence below.
      // Redundant under a decorative root, and kept anyway so the digits stay
      // hidden in their own right rather than only by inheritance.
      "aria-hidden": "true",
    });
    value.textContent = String(correct) + "/" + String(graded);
    node.appendChild(value);

    return node;
  }

  global.LearningStudioIcons = {
    ICONS: ICONS,
    TYPE_ICONS: TYPE_ICONS,
    FALLBACK_ICON: FALLBACK_ICON,
    RING_LENGTH: RING_LENGTH,
    iconFor: iconFor,
    icon: icon,
    ring: ring,
  };
})(typeof window !== "undefined" ? window : this);
