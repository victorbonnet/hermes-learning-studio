/*
 * The card renderers: one per component type in the trusted registry.
 *
 * Three rules govern every function in this file.
 *
 * 1. **Nothing becomes markup.** Every string from the server is written with
 *    `textContent` or as an attribute value on an element this file created.
 *    There is no `innerHTML`, no `insertAdjacentHTML`, no `document.write`, no
 *    `eval`, and no `new Function` -- a contract test greps for all of them,
 *    because the document's Content-Security-Policy cannot stop an injection
 *    that happens through the DOM API. Component text is validated server-side
 *    to be inert, and this file treats it as untrusted anyway: two independent
 *    reasons the same string cannot execute.
 *
 * 2. **`code_response` is text.** It is displayed in a textarea, submitted as a
 *    string, and never passed to anything that could run it. Same for a
 *    learner's own answer, which is read back only as a value.
 *
 * 3. **The card cannot show what the server did not send.** A renderer reads
 *    `component.payload`, which is the stored learner projection: it has no
 *    `answer` and no `evaluation` key to leak, so "do not reveal the answer
 *    before submission" is not a discipline here, it is an absence. `flashcard`
 *    is the visible consequence -- there is no "turn over" button, because the
 *    back of the card is not in the payload and this app has no way to ask for
 *    it.
 *
 * A renderer returns `{ element, read, focus }`. `read()` answers
 * `{ ok: true, response }` or `{ ok: false, error }` with an already-localized
 * message, and the response it produces is the wire shape documented in the
 * README: field names mirror the component's own answer schema so that a later
 * evaluation runtime can compare like with like.
 *
 * Interaction is keyboard-first throughout. Nothing here requires a drag: an
 * ordering card has up/down buttons, a matching card has a `<select>` per row,
 * and the hotspot takes arrow keys as well as a tap. That is not only an
 * accessibility position -- `keyboard_only` is a declarable accommodation, and a
 * renderer that needed a pointer could not honour it.
 */

(function (global) {
  "use strict";

  var MAX_TEXT = 4000; // The API's own per-string ceiling.

  function doc() {
    return global.document;
  }

  /**
   * Create an element. `text` is always assigned to `textContent`; `attrs` are
   * set with `setAttribute`; `props` are assigned as DOM properties (`value`,
   * `checked`, `type`) because those are not attributes once the node is live.
   */
  function el(tag, options) {
    var node = doc().createElement(tag);
    var settings = options || {};
    var key;
    if (settings.className) {
      node.className = settings.className;
    }
    if (settings.text !== undefined && settings.text !== null) {
      node.textContent = String(settings.text);
    }
    if (settings.attrs) {
      for (key in settings.attrs) {
        if (Object.prototype.hasOwnProperty.call(settings.attrs, key)) {
          node.setAttribute(key, String(settings.attrs[key]));
        }
      }
    }
    if (settings.props) {
      for (key in settings.props) {
        if (Object.prototype.hasOwnProperty.call(settings.props, key)) {
          node[key] = settings.props[key];
        }
      }
    }
    if (settings.children) {
      settings.children.forEach(function (child) {
        if (child) {
          node.appendChild(child);
        }
      });
    }
    return node;
  }

  var uniqueCounter = 0;

  function uniqueId(prefix) {
    uniqueCounter += 1;
    return prefix + "-" + uniqueCounter;
  }

  function words(text) {
    var trimmed = String(text || "").trim();
    if (!trimmed) {
      return 0;
    }
    return trimmed.split(/\s+/).length;
  }

  function list(values, className) {
    return el("ul", {
      className: className || "list",
      children: (values || []).map(function (value) {
        return el("li", { text: value });
      }),
    });
  }

  function ok(response) {
    return { ok: true, response: response };
  }

  function bad(message) {
    return { ok: false, error: message };
  }

  // ── Shared building blocks ────────────────────────────────────────────

  /**
   * A radio or checkbox group whose state lives in this closure rather than in
   * the DOM.
   *
   * Reading `input.checked` back out of the tree would work in a browser and
   * only there; keeping the selection here means the same code is exercised by
   * the tests and by the phone. Radios are also un-checked explicitly rather
   * than relying on name-grouping, so the visible state and the state that gets
   * submitted cannot drift apart.
   */
  function choiceGroup(ctx, options, settings) {
    var config = settings || {};
    var groupName = uniqueId("group");
    var selected = config.multiple ? [] : null;
    var inputs = [];

    function isSelected(value) {
      return config.multiple ? selected.indexOf(value) !== -1 : selected === value;
    }

    var labels = options.map(function (option) {
      var input = el("input", {
        props: {
          type: config.multiple ? "checkbox" : "radio",
          name: groupName,
          value: option.value,
          checked: false,
        },
      });
      inputs.push({ input: input, value: option.value });

      input.addEventListener("change", function () {
        if (config.multiple) {
          var at = selected.indexOf(option.value);
          if (input.checked && at === -1) {
            selected.push(option.value);
          } else if (!input.checked && at !== -1) {
            selected.splice(at, 1);
          }
        } else {
          selected = option.value;
          inputs.forEach(function (entry) {
            entry.input.checked = entry.value === option.value;
          });
        }
        if (config.onChange) {
          config.onChange();
        }
      });

      return el("label", {
        className: "choice",
        children: [input, option.node || el("span", { text: option.label })],
      });
    });

    var fieldset = el("fieldset", {
      className: config.grid ? "choice-grid" : "choices",
      children: [
        el("legend", {
          className: config.hideLegend ? "visually-hidden" : "hint",
          text: config.legend,
        }),
      ].concat(labels),
    });

    return {
      node: fieldset,
      selection: function () {
        return config.multiple ? selected.slice() : selected;
      },
      isSelected: isSelected,
      focus: function () {
        if (inputs.length) {
          inputs[0].input.focus();
        }
      },
    };
  }

  /** A labelled `<select>` with an explicit unchosen option. */
  function chooser(ctx, label, options) {
    var id = uniqueId("select");
    var select = el("select", { attrs: { id: id } });
    select.appendChild(el("option", { props: { value: "" }, text: ctx.t("card.unchosen") }));
    options.forEach(function (option) {
      select.appendChild(el("option", { props: { value: option.value }, text: option.label }));
    });
    return {
      node: el("div", {
        className: "field",
        children: [
          el("label", { className: "field-label", attrs: { for: id }, text: label }),
          select,
        ],
      }),
      value: function () {
        return select.value || "";
      },
      focus: function () {
        select.focus();
      },
    };
  }

  /**
   * A single- or multi-line answer field, with a live word count when the
   * component declares a bound.
   */
  function answerField(ctx, settings) {
    var config = settings || {};
    var id = uniqueId("answer");
    var input = config.multiline
      ? el("textarea", {
          className: config.monospace ? "code-input" : "",
          attrs: { id: id, rows: config.rows || 6, maxlength: MAX_TEXT },
          props: { value: config.value || "" },
        })
      : el("input", {
          attrs: { id: id, maxlength: MAX_TEXT, autocomplete: "off" },
          props: { type: "text", value: config.value || "" },
        });

    if (config.monospace) {
      // Autocorrecting somebody's code is worse than not helping at all.
      input.setAttribute("spellcheck", "false");
      input.setAttribute("autocapitalize", "none");
      input.setAttribute("autocomplete", "off");
    }
    if (config.locale) {
      input.setAttribute("lang", config.locale);
    }

    var children = [
      el("label", { className: "field-label", attrs: { for: id }, text: config.label }),
      input,
    ];

    var counter = null;
    if (config.minWords || config.maxWords) {
      counter = el("p", {
        className: "word-count",
        attrs: { "aria-live": "off" },
        text: ctx.t("card.words", { count: 0 }),
      });
      input.addEventListener("input", function () {
        counter.textContent = ctx.t("card.words", { count: words(input.value) });
      });
      children.push(counter);
      if (config.minWords) {
        children.push(el("p", { className: "hint", text: ctx.t("card.min_words", { count: config.minWords }) }));
      }
      if (config.maxWords) {
        children.push(el("p", { className: "hint", text: ctx.t("card.max_words", { count: config.maxWords }) }));
      }
    }

    return {
      node: el("div", { className: "field", children: children }),
      value: function () {
        return String(input.value || "");
      },
      focus: function () {
        input.focus();
      },
      /** `null` when acceptable, otherwise a localized complaint. */
      complaint: function (options) {
        var text = String(input.value || "").trim();
        var required = !options || options.required !== false;
        if (!text) {
          return required ? ctx.t("invalid.required") : null;
        }
        var count = words(text);
        if (config.minWords && count < config.minWords) {
          return ctx.t("invalid.min_words", { count: config.minWords });
        }
        if (config.maxWords && count > config.maxWords) {
          return ctx.t("invalid.max_words", { count: config.maxWords });
        }
        return null;
      },
    };
  }

  /**
   * An `<img>` for a managed asset, plus its text alternatives.
   *
   * The URL is not in the payload and is never built from one: `ctx.loadImage`
   * is the only way to a picture, it takes an opaque `asset_ref`, and it is the
   * application that turns that into a same-origin request the server
   * authorises against this session's own experience. A renderer therefore
   * *cannot* point an `<img>` at an arbitrary address, however a manifest were
   * written.
   *
   * `alt_text` is mandatory in the registry, so there is always something to
   * show when the bytes do not arrive -- which is what the failure branch does
   * rather than leaving an empty frame.
   */
  function figure(ctx, asset, settings) {
    var config = settings || {};
    var image = el("img", {
      attrs: { alt: asset.alt_text || "", decoding: "async" },
    });
    var status = el("p", { className: "image-status", text: ctx.t("card.image_loading") });
    var holder = el("div", { children: [status] });

    ctx.loadImage(asset.asset_ref).then(
      function (url) {
        image.setAttribute("src", url);
        holder.replaceChildren(image);
      },
      function () {
        status.textContent = ctx.t("card.image_failed");
      }
    );

    var extras = [];
    if (asset.long_description) {
      extras.push(
        el("details", {
          children: [
            el("summary", { text: ctx.t("card.long_description") }),
            el("p", { className: "note", text: asset.long_description }),
          ],
        })
      );
    }
    if (config.caption) {
      extras.push(el("figcaption", { text: config.caption }));
    }

    return {
      node: el("figure", { className: "figure", children: [holder].concat(extras) }),
      surface: holder,
      image: image,
    };
  }

  /** A reorderable list driven by buttons, never by dragging. */
  function orderable(ctx, items, hintKey) {
    var order = items.slice();
    var container = el("ol", { className: "ordered" });

    function move(index, delta) {
      var target = index + delta;
      if (target < 0 || target >= order.length) {
        return;
      }
      var moved = order[index];
      order[index] = order[target];
      order[target] = moved;
      draw(target);
    }

    function draw(focusIndex) {
      container.replaceChildren();
      order.forEach(function (item, index) {
        var up = el("button", {
          className: "small",
          text: "↑",
          attrs: {
            type: "button",
            "aria-label": ctx.t("card.move_up") + ": " + item.label,
          },
          props: { disabled: index === 0 },
        });
        var down = el("button", {
          className: "small",
          text: "↓",
          attrs: {
            type: "button",
            "aria-label": ctx.t("card.move_down") + ": " + item.label,
          },
          props: { disabled: index === order.length - 1 },
        });
        up.addEventListener("click", function () {
          move(index, -1);
        });
        down.addEventListener("click", function () {
          move(index, 1);
        });

        container.appendChild(
          el("li", {
            children: [
              el("span", {
                className: "rank",
                text: String(index + 1),
                attrs: { "aria-label": ctx.t("card.position", { position: index + 1 }) },
              }),
              el("span", { className: "text", text: item.label }),
              el("span", { className: "move-buttons", children: [up, down] }),
            ],
          })
        );
      });
      if (focusIndex !== undefined && focusIndex !== null) {
        // Focus follows the item that moved, so a keyboard user can press the
        // same button again instead of hunting for where the row went.
        var row = container.children[focusIndex];
        if (row) {
          var buttons = row.children[2];
          var wanted = buttons && buttons.children[focusIndex === 0 ? 1 : 0];
          if (wanted && !wanted.disabled) {
            wanted.focus();
          }
        }
      }
    }

    draw(null);

    return {
      node: el("div", {
        children: [el("p", { className: "hint", text: ctx.t(hintKey) }), container],
      }),
      order: function () {
        return order.map(function (item) {
          return item.id;
        });
      },
    };
  }

  function orderingRenderer(field, labelOf, hintKey) {
    return function (component, ctx) {
      var entries = (component.content[field] || []).map(function (entry) {
        return { id: entry.id, label: labelOf(entry) };
      });
      var control = orderable(ctx, entries, hintKey);
      return {
        body: [control.node],
        read: function () {
          return ok({ order: control.order() });
        },
      };
    };
  }

  function singleChoiceRenderer(build) {
    return function (component, ctx) {
      var parts = build(component, ctx);
      var group = choiceGroup(ctx, parts.options, {
        legend: parts.legend || component.prompt,
        hideLegend: true,
        grid: parts.grid,
      });
      return {
        body: (parts.before || []).concat([group.node]),
        focus: group.focus,
        read: function () {
          var chosen = group.selection();
          if (!chosen) {
            return bad(ctx.t("invalid.choose_one"));
          }
          return ok(parts.toResponse ? parts.toResponse(chosen) : { option_id: chosen });
        },
      };
    };
  }

  function longTextRenderer(settings) {
    var config = settings || {};
    return function (component, ctx) {
      var before = config.before ? config.before(component, ctx) : [];
      var field = answerField(ctx, {
        label: ctx.t(config.labelKey || "card.answer_label"),
        multiline: config.multiline !== false,
        monospace: config.monospace,
        rows: config.rows,
        value: config.initial ? config.initial(component) : "",
        minWords: component.content.min_words,
        maxWords: component.content.max_words,
        locale: config.locale ? config.locale(component) : null,
      });
      return {
        body: before.concat([field.node]),
        focus: field.focus,
        read: function () {
          var complaint = field.complaint();
          if (complaint) {
            return bad(complaint);
          }
          var response = {};
          response[config.responseKey || "text"] = field.value();
          return ok(response);
        },
      };
    };
  }

  /**
   * One `<textarea>` per prompt, for the open-response families.
   *
   * `min_words` is a property of the component, not of each prompt, so it is
   * checked against the total. Applying it per field would silently multiply the
   * requirement -- a reflection with two prompts and `min_words: 40` would
   * demand eighty words from somebody who was told forty.
   */
  function promptListRenderer(field, headingKey) {
    return function (component, ctx) {
      var prompts = component.content[field] || [];
      var minimum = component.content.min_words;
      var fields = prompts.map(function (prompt) {
        return answerField(ctx, { label: prompt, multiline: true, rows: 4 });
      });

      var before = [];
      if (headingKey && prompts.length > 1) {
        before.push(el("p", { className: "hint", text: ctx.t(headingKey) }));
      }
      if (minimum) {
        before.push(el("p", { className: "hint", text: ctx.t("card.min_words", { count: minimum }) }));
      }

      return {
        body: before.concat(
          fields.map(function (entry) {
            return entry.node;
          })
        ),
        focus: fields.length ? fields[0].focus : null,
        read: function () {
          var written = [];
          for (var index = 0; index < fields.length; index += 1) {
            var complaint = fields[index].complaint();
            if (complaint) {
              return bad(fields.length > 1 ? ctx.t("invalid.all_prompts") : complaint);
            }
            written.push(fields[index].value());
          }
          if (minimum && words(written.join(" ")) < minimum) {
            return bad(ctx.t("invalid.min_words", { count: minimum }));
          }
          return ok({ responses: written });
        },
      };
    };
  }

  // ── The registry ──────────────────────────────────────────────────────

  var RENDERERS = {};

  // Selection ------------------------------------------------------------

  RENDERERS.multiple_choice = singleChoiceRenderer(function (component) {
    return {
      options: component.content.options.map(function (option) {
        return { value: option.id, label: option.text };
      }),
    };
  });

  RENDERERS.multi_select = function (component, ctx) {
    var group = choiceGroup(
      ctx,
      component.content.options.map(function (option) {
        return { value: option.id, label: option.text };
      }),
      { legend: ctx.t("card.multi_hint"), multiple: true }
    );
    return {
      body: [group.node],
      focus: group.focus,
      read: function () {
        var chosen = group.selection();
        if (!chosen.length) {
          return bad(ctx.t("invalid.choose_some"));
        }
        return ok({ option_ids: chosen });
      },
    };
  };

  RENDERERS.true_false = function (component, ctx) {
    var group = choiceGroup(
      ctx,
      [
        { value: "true", label: ctx.t("card.true") },
        { value: "false", label: ctx.t("card.false") },
      ],
      { legend: ctx.t("card.statement"), hideLegend: false }
    );
    return {
      body: [el("p", { className: "statement", text: component.content.statement }), group.node],
      focus: group.focus,
      read: function () {
        var chosen = group.selection();
        if (chosen === null) {
          return bad(ctx.t("invalid.choose_one"));
        }
        return ok({ value: chosen === "true" });
      },
    };
  };

  /** One category per item: `classification` picks one, `categorization` may
   * allow several, and both submit the same `assignments` shape. */
  function bucketRenderer(settings) {
    return function (component, ctx) {
      var categories = component.content.categories.map(function (category) {
        return { value: category.id, label: category.label };
      });
      var multiple = settings.multiple && component.content.allow_multiple === true;

      var controls = component.content.items.map(function (item) {
        if (multiple) {
          var group = choiceGroup(ctx, categories, {
            legend: ctx.t("card.category_for", { text: item.text }),
            multiple: true,
          });
          return {
            item: item,
            node: el("div", {
              className: "pair",
              children: [el("span", { className: "text", text: item.text }), group.node],
            }),
            values: group.selection,
          };
        }
        var select = chooser(ctx, ctx.t("card.category_for", { text: item.text }), categories);
        return {
          item: item,
          node: el("div", { className: "pair", children: [select.node] }),
          values: function () {
            var value = select.value();
            return value ? [value] : [];
          },
        };
      });

      return {
        body: [
          el("p", { className: "hint", text: ctx.t("card.categorization_hint") }),
          el("div", {
            className: "pairs",
            children: controls.map(function (control) {
              return control.node;
            }),
          }),
        ],
        read: function () {
          var assignments = [];
          for (var index = 0; index < controls.length; index += 1) {
            var chosen = controls[index].values();
            if (!chosen.length) {
              return bad(ctx.t("invalid.all_items"));
            }
            assignments.push(
              settings.multiple
                ? { item_id: controls[index].item.id, category_ids: chosen }
                : { item_id: controls[index].item.id, category_id: chosen[0] }
            );
          }
          return ok({ assignments: assignments });
        },
      };
    };
  }

  RENDERERS.classification = bucketRenderer({ multiple: false });
  RENDERERS.categorization = bucketRenderer({ multiple: true });

  RENDERERS.image_choice = function (component, ctx) {
    var group = choiceGroup(
      ctx,
      component.content.options.map(function (option) {
        var picture = figure(ctx, option.image, { caption: option.caption });
        return {
          value: option.id,
          node: el("span", { children: [picture.node] }),
        };
      }),
      { legend: component.prompt, hideLegend: true, grid: true }
    );
    return {
      body: [group.node],
      focus: group.focus,
      read: function () {
        var chosen = group.selection();
        if (!chosen) {
          return bad(ctx.t("invalid.choose_one"));
        }
        return ok({ option_id: chosen });
      },
    };
  };

  RENDERERS.scenario_choice = singleChoiceRenderer(function (component) {
    return {
      before: [el("p", { className: "situation", text: component.content.situation })],
      options: component.content.options.map(function (option) {
        return { value: option.id, label: option.text };
      }),
    };
  });

  RENDERERS.decision_path = function (component, ctx) {
    var steps = component.content.steps.map(function (step, index) {
      var select = chooser(
        ctx,
        ctx.t("card.step_number", { number: index + 1 }) + " — " + step.prompt,
        step.options.map(function (option) {
          return { value: option.id, label: option.text };
        })
      );
      return { step: step, select: select };
    });
    return {
      body: [
        el("p", { className: "situation", text: component.content.situation }),
        el("div", {
          className: "pairs",
          children: steps.map(function (entry) {
            return el("div", { className: "pair", children: [entry.select.node] });
          }),
        }),
      ],
      read: function () {
        var decisions = [];
        for (var index = 0; index < steps.length; index += 1) {
          var value = steps[index].select.value();
          if (!value) {
            return bad(ctx.t("invalid.all_steps"));
          }
          decisions.push({ step_id: steps[index].step.id, option_id: value });
        }
        return ok({ decisions: decisions });
      },
    };
  };

  RENDERERS.case_study = function (component, ctx) {
    var render = promptListRenderer("questions", "card.questions");
    var built = render(component, ctx);
    var before = [el("p", { className: "passage", text: component.content.background })];
    if (component.content.materials) {
      before.push(el("p", { className: "hint", text: ctx.t("card.materials") }));
      before.push(list(component.content.materials));
    }
    built.body = before.concat(built.body);
    return built;
  };

  // Text input -----------------------------------------------------------

  var PLACEHOLDER = /\{\{\s*([a-z0-9_-]+)\s*\}\}/;

  RENDERERS.fill_blank = function (component, ctx) {
    var labels = {};
    component.content.blanks.forEach(function (blank, index) {
      labels[blank.id] = blank.label
        ? ctx.t("card.blank_label", { label: blank.label })
        : ctx.t("card.blank_number", { number: index + 1 });
    });

    // The passage is split on its own placeholder syntax and reassembled out of
    // text spans and inputs. Building an HTML string with `<input>` in it would
    // have been shorter and would have made every passage an injection point.
    var pieces = String(component.content.text).split(
      new RegExp(PLACEHOLDER.source, "g")
    );
    var paragraph = el("p", { className: "passage" });
    var fields = [];

    pieces.forEach(function (piece, index) {
      if (index % 2 === 0) {
        if (piece) {
          paragraph.appendChild(el("span", { text: piece }));
        }
        return;
      }
      var id = uniqueId("blank");
      var input = el("input", {
        className: "blank",
        attrs: {
          id: id,
          "aria-label": labels[piece] || piece,
          maxlength: 200,
          size: 12,
          autocomplete: "off",
        },
        props: { type: "text" },
      });
      fields.push({ blank_id: piece, input: input });
      paragraph.appendChild(input);
    });

    return {
      body: [paragraph],
      focus: fields.length
        ? function () {
            fields[0].input.focus();
          }
        : null,
      read: function () {
        var blanks = [];
        for (var index = 0; index < fields.length; index += 1) {
          var text = String(fields[index].input.value || "").trim();
          if (!text) {
            return bad(ctx.t("invalid.all_blanks"));
          }
          blanks.push({ blank_id: fields[index].blank_id, text: text });
        }
        return ok({ blanks: blanks });
      },
    };
  };

  RENDERERS.short_answer = longTextRenderer({ multiline: false });
  RENDERERS.typed_recall = longTextRenderer({
    multiline: false,
    before: function (component) {
      return [el("p", { className: "statement", text: component.content.cue })];
    },
  });
  RENDERERS.free_response = longTextRenderer({ rows: 8 });
  RENDERERS.rubric_response = longTextRenderer({
    rows: 8,
    before: function (component, ctx) {
      if (!component.content.requirements) {
        return [];
      }
      return [el("p", { className: "hint", text: ctx.t("card.requirements") }), list(component.content.requirements)];
    },
  });

  RENDERERS.translation = longTextRenderer({
    rows: 4,
    before: function (component) {
      return [
        el("p", {
          className: "statement",
          text: component.content.source_text,
          attrs: { lang: component.content.source_locale },
        }),
      ];
    },
    locale: function (component) {
      return component.content.target_locale;
    },
  });

  RENDERERS.error_correction = longTextRenderer({
    rows: 6,
    // Seeded with the flawed passage: the task is to correct it, and retyping
    // it from scratch tests transcription rather than the skill in question.
    initial: function (component) {
      return component.content.text;
    },
    before: function (component, ctx) {
      return [el("p", { className: "hint", text: ctx.t("card.error_correction_hint") })];
    },
  });

  RENDERERS.code_response = function (component, ctx) {
    var field = answerField(ctx, {
      label: ctx.t("card.code_label", { language: component.content.language }),
      multiline: true,
      monospace: true,
      rows: 10,
      value: component.content.starter_code || "",
    });
    var before = [el("p", { className: "notice", text: ctx.t("card.code_notice") })];
    if (component.content.requirements) {
      before.push(el("p", { className: "hint", text: ctx.t("card.requirements") }));
      before.push(list(component.content.requirements));
    }
    return {
      body: before.concat([field.node]),
      focus: field.focus,
      read: function () {
        var complaint = field.complaint();
        if (complaint) {
          return bad(complaint);
        }
        // Submitted as a string, to be stored as a string. Nothing in this
        // application parses, compiles, or executes it.
        return ok({ code: field.value() });
      },
    };
  };

  // Ordering -------------------------------------------------------------

  RENDERERS.sentence_order = orderingRenderer(
    "tokens",
    function (token) {
      return token.text;
    },
    "card.order_hint"
  );

  RENDERERS.sequence_order = orderingRenderer(
    "steps",
    function (step) {
      return step.text;
    },
    "card.order_hint"
  );

  RENDERERS.timeline = orderingRenderer(
    "events",
    function (event) {
      return event.date_label ? event.date_label + " — " + event.text : event.text;
    },
    "card.timeline_hint"
  );

  RENDERERS.process_flow = function (component, ctx) {
    var built = orderingRenderer(
      "stages",
      function (stage) {
        return stage.text;
      },
      "card.order_hint"
    )(component, ctx);
    if (component.content.start_stage_label) {
      built.body = [
        el("p", { className: "hint", text: component.content.start_stage_label }),
      ].concat(built.body);
    }
    return built;
  };

  RENDERERS.matching = function (component, ctx) {
    var right = component.content.right.map(function (entry) {
      return { value: entry.id, label: entry.text };
    });
    var rows = component.content.left.map(function (entry) {
      var select = chooser(ctx, ctx.t("card.match_for", { text: entry.text }), right);
      return { left: entry, select: select };
    });
    return {
      body: [
        el("div", {
          className: "pairs",
          children: rows.map(function (row) {
            return el("div", { className: "pair", children: [row.select.node] });
          }),
        }),
      ],
      read: function () {
        var pairs = [];
        for (var index = 0; index < rows.length; index += 1) {
          var value = rows[index].select.value();
          if (!value) {
            return bad(ctx.t("invalid.all_pairs"));
          }
          pairs.push({ left_id: rows[index].left.id, right_id: value });
        }
        return ok({ pairs: pairs });
      },
    };
  };

  // Recall ---------------------------------------------------------------

  var RATINGS = ["again", "hard", "good", "easy"];

  RENDERERS.flashcard = function (component, ctx) {
    // No reveal button: the back of the card lives in the evaluator-only half
    // of the component and never reaches this app. What a learner can do here
    // is write down what they remember and say how it went.
    var recall = answerField(ctx, { label: ctx.t("card.recall_label"), multiline: true, rows: 3 });
    var rating = choiceGroup(
      ctx,
      RATINGS.map(function (value) {
        return { value: value, label: ctx.t("rate." + value) };
      }),
      { legend: ctx.t("card.self_rate"), grid: true }
    );

    var front = [el("p", { className: "statement", text: component.content.front })];
    if (component.content.front_note) {
      front.push(el("p", { className: "note", text: component.content.front_note }));
    }

    return {
      body: front.concat([recall.node, rating.node]),
      focus: recall.focus,
      read: function () {
        var chosen = rating.selection();
        if (!chosen) {
          return bad(ctx.t("invalid.rating"));
        }
        return ok({ text: recall.value(), self_rating: chosen });
      },
    };
  };

  // Visual ---------------------------------------------------------------

  RENDERERS.image_observation = function (component, ctx) {
    var picture = figure(ctx, component.content.image);
    var field = answerField(ctx, { label: ctx.t("card.answer_label"), multiline: true, rows: 6 });
    var before = [picture.node];
    if (component.content.focus_points) {
      before.push(el("p", { className: "hint", text: ctx.t("card.focus_points") }));
      before.push(list(component.content.focus_points));
    }
    return {
      body: before.concat([field.node]),
      focus: field.focus,
      read: function () {
        var complaint = field.complaint();
        return complaint ? bad(complaint) : ok({ text: field.value() });
      },
    };
  };

  RENDERERS.diagram = function (component, ctx) {
    var picture = figure(ctx, component.content.image);
    var field = answerField(ctx, { label: ctx.t("card.answer_label"), multiline: true, rows: 4 });
    var before = [picture.node];
    if (component.content.callouts) {
      before.push(
        list(
          component.content.callouts.map(function (callout) {
            return callout.text;
          })
        )
      );
    }
    return {
      body: before.concat([field.node]),
      focus: field.focus,
      read: function () {
        var complaint = field.complaint();
        return complaint ? bad(complaint) : ok({ text: field.value() });
      },
    };
  };

  /**
   * Normalised-coordinate hotspot.
   *
   * The point submitted is a fraction of the image's own width and height, so
   * it means the same thing on any screen and at any zoom -- the answer key is
   * in those units too. Two ways in: tap the surface, or focus it and use the
   * arrow keys, which start the marker at the centre and step by 5% (1% with
   * Shift). A card that only accepted a tap could not honour `keyboard_only`.
   */
  RENDERERS.hotspot = function (component, ctx) {
    var picture = figure(ctx, component.content.image);
    var point = null;

    var marker = el("span", { className: "marker", attrs: { "aria-hidden": "true" } });
    marker.hidden = true;

    var surface = el("button", {
      className: "hotspot",
      attrs: { type: "button", "aria-label": ctx.t("card.hotspot_pick") },
      children: [picture.node, marker],
    });
    if (component.content.show_grid) {
      surface.appendChild(el("span", { className: "hotspot-grid", attrs: { "aria-hidden": "true" } }));
    }

    var readout = el("p", { className: "hint", attrs: { "aria-live": "polite" } });
    var clear = el("button", {
      className: "small",
      text: ctx.t("card.hotspot_clear"),
      attrs: { type: "button" },
      props: { disabled: true },
    });

    function place(x, y) {
      point = { x: Math.min(1, Math.max(0, x)), y: Math.min(1, Math.max(0, y)) };
      marker.hidden = false;
      marker.style.left = point.x * 100 + "%";
      marker.style.top = point.y * 100 + "%";
      clear.disabled = false;
      readout.textContent = ctx.t("card.hotspot_selected", {
        x: Math.round(point.x * 100),
        y: Math.round(point.y * 100),
      });
    }

    surface.addEventListener("click", function (event) {
      var box = surface.getBoundingClientRect ? surface.getBoundingClientRect() : null;
      if (!box || !box.width || !box.height) {
        return;
      }
      place((event.clientX - box.left) / box.width, (event.clientY - box.top) / box.height);
    });

    var STEPS = {
      ArrowLeft: [-1, 0],
      ArrowRight: [1, 0],
      ArrowUp: [0, -1],
      ArrowDown: [0, 1],
    };

    surface.addEventListener("keydown", function (event) {
      var step = STEPS[event.key];
      if (!step) {
        return;
      }
      if (event.preventDefault) {
        event.preventDefault();
      }
      var size = event.shiftKey ? 0.01 : 0.05;
      var from = point || { x: 0.5, y: 0.5 };
      place(from.x + step[0] * size, from.y + step[1] * size);
    });

    clear.addEventListener("click", function () {
      point = null;
      marker.hidden = true;
      clear.disabled = true;
      readout.textContent = "";
    });

    return {
      body: [
        el("p", { className: "hint", text: ctx.t("card.hotspot_hint") }),
        el("p", { className: "hint", text: ctx.t("card.hotspot_keyboard") }),
        surface,
        readout,
        clear,
      ],
      focus: function () {
        surface.focus();
      },
      read: function () {
        if (!point) {
          return bad(ctx.t("invalid.point"));
        }
        return ok({
          points: [{ x: Number(point.x.toFixed(4)), y: Number(point.y.toFixed(4)) }],
        });
      },
    };
  };

  RENDERERS.labeling = function (component, ctx) {
    var picture = figure(ctx, component.content.image);
    var surface = el("div", { className: "hotspot", children: [picture.node] });

    component.content.markers.forEach(function (mark, index) {
      var pin = el("span", {
        className: "marker",
        text: String(index + 1),
        attrs: { "aria-hidden": "true" },
      });
      pin.style.left = mark.x * 100 + "%";
      pin.style.top = mark.y * 100 + "%";
      surface.appendChild(pin);
    });

    var bank = component.content.label_bank.map(function (label) {
      return { value: label.id, label: label.text };
    });
    var rows = component.content.markers.map(function (mark, index) {
      return {
        mark: mark,
        select: chooser(ctx, ctx.t("card.marker_label", { number: index + 1 }), bank),
      };
    });

    return {
      body: [
        surface,
        el("div", {
          className: "pairs",
          children: rows.map(function (row) {
            return el("div", { className: "pair", children: [row.select.node] });
          }),
        }),
      ],
      read: function () {
        var labels = [];
        for (var index = 0; index < rows.length; index += 1) {
          var value = rows[index].select.value();
          if (!value) {
            return bad(ctx.t("invalid.all_markers"));
          }
          labels.push({ marker_id: rows[index].mark.id, label_id: value });
        }
        return ok({ labels: labels });
      },
    };
  };

  // Structured -----------------------------------------------------------

  RENDERERS.table_grid = function (component, ctx) {
    var prefilled = {};
    (component.content.prefilled_cells || []).forEach(function (cell) {
      prefilled[cell.row_id + " " + cell.column_id] = cell.text;
    });

    var head = el("tr", {
      children: [el("th", { attrs: { scope: "col" }, text: "" })].concat(
        component.content.columns.map(function (column) {
          return el("th", { attrs: { scope: "col" }, text: column.header });
        })
      ),
    });

    var cells = [];
    var rows = component.content.rows.map(function (row) {
      var tds = component.content.columns.map(function (column) {
        var given = prefilled[row.id + " " + column.id];
        if (given !== undefined) {
          return el("td", { text: given });
        }
        var input = el("input", {
          attrs: {
            type: "text",
            maxlength: 200,
            autocomplete: "off",
            "aria-label": ctx.t("card.grid_cell", { row: row.header, column: column.header }),
          },
          props: { type: "text" },
        });
        cells.push({ row_id: row.id, column_id: column.id, input: input });
        return el("td", { children: [input] });
      });
      return el("tr", {
        children: [el("th", { attrs: { scope: "row" }, text: row.header })].concat(tds),
      });
    });

    return {
      body: [
        el("div", {
          className: "grid-scroll",
          children: [
            el("table", {
              className: "grid",
              children: [el("thead", { children: [head] }), el("tbody", { children: rows })],
            }),
          ],
        }),
      ],
      read: function () {
        var filled = [];
        for (var index = 0; index < cells.length; index += 1) {
          var text = String(cells[index].input.value || "").trim();
          if (!text) {
            return bad(ctx.t("invalid.all_cells"));
          }
          filled.push({
            row_id: cells[index].row_id,
            column_id: cells[index].column_id,
            text: text,
          });
        }
        return ok({ cells: filled });
      },
    };
  };

  // Reflection -----------------------------------------------------------

  RENDERERS.confidence_rating = function (component, ctx) {
    var labels = component.content.scale_labels || [];
    var options = [];
    for (var value = component.content.scale_min; value <= component.content.scale_max; value += 1) {
      var index = value - component.content.scale_min;
      options.push({
        value: String(value),
        label: labels[index] ? value + " — " + labels[index] : String(value),
      });
    }
    var group = choiceGroup(ctx, options, { legend: ctx.t("card.confidence") });
    return {
      body: [group.node],
      focus: group.focus,
      read: function () {
        var chosen = group.selection();
        if (chosen === null) {
          return bad(ctx.t("invalid.rating"));
        }
        return ok({ rating: parseInt(chosen, 10) });
      },
    };
  };

  RENDERERS.reflection = promptListRenderer("prompts", null);
  RENDERERS.self_explanation = promptListRenderer("prompts", null);

  // ── Dispatch ──────────────────────────────────────────────────────────

  var SUPPORTED_TYPES = Object.keys(RENDERERS).sort();

  /**
   * The card for a type this build does not know.
   *
   * A registry the server validates against can legitimately be ahead of a
   * frontend somebody has not updated, and the honest response to that is a
   * card saying so -- not a blank screen, and not a guess at how to display
   * something whose shape this code has never seen. `read()` refuses, so an
   * unknown card cannot be submitted as an empty answer either.
   */
  function unsupported(component, ctx) {
    return {
      body: [
        el("h3", { text: ctx.t("card.unsupported.title") }),
        el("p", { text: ctx.t("card.unsupported.body") }),
        el("p", { className: "hint", text: ctx.t("card.unsupported.type", { type: component.type }) }),
      ],
      unsupported: true,
      read: function () {
        return bad(ctx.t("card.unsupported.title"));
      },
    };
  }

  /** The accessibility metadata that travels with a component, made visible. */
  function alternatives(component, ctx) {
    var access = component.accessibility || {};
    var nodes = [];
    if (access.caption) {
      nodes.push(el("p", { className: "note", text: access.caption }));
    }
    if (access.keyboard_alternative) {
      nodes.push(
        el("p", {
          className: "hint",
          text: ctx.t("card.keyboard_alternative", { text: access.keyboard_alternative }),
        })
      );
    }
    if (access.long_description) {
      nodes.push(
        el("details", {
          children: [
            el("summary", { text: ctx.t("card.long_description") }),
            el("p", { className: "note", text: access.long_description }),
          ],
        })
      );
    }
    if (access.transcript) {
      nodes.push(
        el("details", {
          children: [
            el("summary", { text: ctx.t("card.transcript") }),
            el("p", { className: "note", text: access.transcript }),
          ],
        })
      );
    }
    return nodes;
  }

  /**
   * Render one component of the API's `component` shape.
   *
   * `payload` is the stored learner projection; `type` is the discriminator.
   * The returned object is what the application drives: an element to show, a
   * `read()` to submit, and an optional `focus()` for when the card appears.
   */
  function render(component, ctx) {
    var payload = component.payload || {};
    var view = {
      type: component.type,
      prompt: payload.prompt || "",
      content: payload.content || {},
      accessibility: payload.accessibility || {},
    };

    var builder = Object.prototype.hasOwnProperty.call(RENDERERS, component.type)
      ? RENDERERS[component.type]
      : unsupported;

    var built;
    try {
      built = builder(view, ctx);
    } catch (error) {
      // A malformed payload must cost one card, not the session. There is
      // nothing useful to say about the failure to a learner, and the type is
      // the one thing that is safe to name.
      built = unsupported(view, ctx);
    }

    var section = el("section", {
      className: "state",
      attrs: { "data-component-type": component.type },
      children: [el("h2", { className: "prompt", text: view.prompt })]
        .concat(alternatives(view, ctx))
        .concat(built.body),
    });

    if (view.accessibility.reduced_motion) {
      section.setAttribute("data-reduced-motion", "true");
    }

    return {
      element: section,
      unsupported: built.unsupported === true,
      read: built.read,
      focus: built.focus || null,
    };
  }

  global.LearningStudioRenderers = {
    RENDERERS: RENDERERS,
    SUPPORTED_TYPES: SUPPORTED_TYPES,
    render: render,
    words: words,
    el: el,
  };
})(typeof window !== "undefined" ? window : this);
