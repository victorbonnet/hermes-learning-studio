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
 *    before submission" is not a discipline here, it is an absence.
 *
 *    `flashcard` is where that absence is visible. Its "turn over" button does
 *    not uncover text the page was already holding -- there is none. It asks the
 *    server, through `ctx.reveal`, which grants the back of the card only after
 *    an attempt has been committed and then freezes that attempt. The renderer
 *    cannot reach the network itself and cannot obtain the answer any other way.
 *
 *    The same absence explains the shuffling that is *not* here: an ordering
 *    card's list arrives already rearranged, because the server rearranges it
 *    while building the projection. A frontend that received the correct order
 *    and was trusted to scramble it would be one bug away from displaying the
 *    answer, and the bug would be invisible.
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

  //: The icon set, loaded before this file. Read through a variable rather than
  //: reached for at each call site so that a build where it is missing fails in
  //: one obvious place instead of thirty-one obscure ones.
  var Icons = global.LearningStudioIcons;

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

  //: A BCP-47-ish language tag, matching what the manifest validator accepts.
  //: An unrecognised value is left off rather than written to the DOM: a bogus
  //: `lang` is worse than none, because a screen reader will act on it.
  var LOCALE = /^[a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-(?:[A-Z]{2}|[0-9]{3}))?$/;

  /**
   * The language to mark exercise content with, or `null`.
   *
   * `null` when the exercise is written in the reader's own interface language,
   * because in that case every `lang` attribute would be noise, and when the tag
   * is missing or malformed.
   */
  function contentLang(ctx) {
    var locale = ctx.contentLocale;
    if (typeof locale !== "string" || !LOCALE.test(locale) || locale === ctx.uiLocale) {
      return null;
    }
    return locale;
  }

  /**
   * An element holding *exercise content*, tagged with the content language.
   *
   * The distinction this exists to keep: a French verb drill is in French
   * whoever is studying it, and a screen reader has to be told so or it will
   * pronounce the French with English rules. The interface around it belongs to
   * the reader and stays in their language.
   */
  function content(ctx, tag, options) {
    var node = el(tag, options);
    var locale = contentLang(ctx);
    if (locale) {
      node.setAttribute("lang", locale);
    }
    return node;
  }

  /**
   * An element holding *interface chrome*, tagged with the UI language.
   *
   * Only needed when the card as a whole is marked with a different language:
   * these elements sit inside a container carrying `lang="fr"` and would
   * otherwise inherit it, which would be wrong in the other direction.
   */
  function chrome(ctx, tag, options) {
    var node = el(tag, options);
    if (contentLang(ctx)) {
      node.setAttribute("lang", ctx.uiLocale || "en");
    }
    return node;
  }

  /** The most common piece of chrome: a localized hint or notice. */
  function hint(ctx, key, values, className) {
    return chrome(ctx, "p", { className: className || "hint", text: ctx.t(key, values) });
  }

  function words(text) {
    var trimmed = String(text || "").trim();
    if (!trimmed) {
      return 0;
    }
    return trimmed.split(/\s+/).length;
  }

  /** A bulleted list of authored content, in the exercise's own language. */
  function list(ctx, values, className) {
    return content(ctx, "ul", {
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
        children: [
          input,
          option.node || content(ctx, "span", { text: option.label }),
        ],
      });
    });

    var fieldset = el("fieldset", {
      className: config.grid ? "choice-grid" : "choices",
      children: [
        // The legend is a hint the interface wrote, unless the caller says it is
        // the component's own prompt repeated for the group.
        (config.legendIsContent ? content : chrome)(ctx, "legend", {
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
    select.appendChild(chrome(ctx, "option", { props: { value: "" }, text: ctx.t("card.unchosen") }));
    options.forEach(function (option) {
      select.appendChild(content(ctx, "option", { props: { value: option.value }, text: option.label }));
    });
    return {
      node: el("div", {
        className: "field",
        children: [
          chrome(ctx, "label", { className: "field-label", attrs: { for: id }, text: label }),
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
      (config.labelIsContent ? content : chrome)(ctx, "label", {
        className: "field-label",
        attrs: { for: id },
        text: config.label,
      }),
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
        children.push(hint(ctx, "card.min_words", { count: config.minWords }));
      }
      if (config.maxWords) {
        children.push(hint(ctx, "card.max_words", { count: config.maxWords }));
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
      /**
       * Pin the field to a value and stop it being edited.
       *
       * Used when a flashcard is turned over. The server has already frozen the
       * attempt at that moment and will refuse a submission that disagrees with
       * it, so leaving the box editable would only invite a learner to write
       * something that then gets rejected.
       */
      freeze: function (value) {
        if (value !== undefined && value !== null) {
          input.value = String(value);
        }
        input.readOnly = true;
        input.setAttribute("readonly", "readonly");
        input.setAttribute("aria-readonly", "true");
      },
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
    var altText = String(asset.alt_text || "");
    var fallbackId = uniqueId("image-alt");

    var image = el("img", { attrs: { alt: altText, decoding: "async" } });
    var holder = el("div", { children: [hint(ctx, "card.image_loading", null, "image-status")] });

    /**
     * Show the text alternative, visibly, in place of the picture.
     *
     * This is the whole point of `alt_text` being mandatory in the registry, and
     * the first version of this code threw it away: it said "its description is
     * below" and then put nothing below unless the author had *also* written a
     * long description. A learner met an empty frame and a promise.
     *
     * The alt text is rendered as ordinary text rather than left on a broken
     * `<img>`, because a broken image's `alt` is displayed inconsistently, is
     * unstyleable, and disappears entirely in some clients. It keeps the content
     * language: it is the exercise's own prose.
     */
    function showAlternative() {
      holder.replaceChildren(
        hint(ctx, "card.image_failed", null, "image-status"),
        content(ctx, "p", {
          className: "image-alt",
          text: altText,
          attrs: { id: fallbackId },
        })
      );
      if (config.onFallback) {
        // A control that depends on the picture wants to describe itself with
        // the text alternative instead — but only now that there is one. Naming
        // the id up front would leave a dangling `aria-describedby`, which is a
        // reference to nothing rather than a reference to the description.
        config.onFallback(fallbackId);
      }
    }

    // Two ways an image fails, and only one of them is a failed request. A
    // response that arrives and then will not decode — truncated bytes, a
    // mislabelled file — fires `error` on the element instead, and the first
    // version handled neither that nor the styling consequences.
    image.addEventListener("error", showAlternative);

    ctx.loadImage(asset.asset_ref).then(function (url) {
      image.setAttribute("src", url);
      holder.replaceChildren(image);
    }, showAlternative);

    var extras = [];
    if (config.caption) {
      // A caption is authored content, not chrome.
      extras.push(content(ctx, "figcaption", { text: config.caption }));
    }
    if (asset.long_description) {
      // Kept separate from the alt text on purpose: one is the concise
      // alternative every reader may need, the other is the long form somebody
      // chooses to open. Collapsing them would mean a reader who wanted the short
      // version got an essay, and a reader who wanted the detail got nothing.
      extras.push(
        el("details", {
          children: [
            chrome(ctx, "summary", { text: ctx.t("card.long_description") }),
            content(ctx, "p", { className: "note", text: asset.long_description }),
          ],
        })
      );
    }

    return {
      node: el("figure", { className: "figure", children: [holder].concat(extras) }),
      /** The element the picture occupies — never the prose around it. */
      holder: holder,
      image: image,
      /** For `aria-describedby` on a control that depends on the image. */
      fallbackId: fallbackId,
      extras: extras,
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
              content(ctx, "span", { className: "text", text: item.label }),
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
        children: [hint(ctx, hintKey), container],
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
        legendIsContent: true,
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
        return answerField(ctx, { label: prompt, multiline: true, rows: 4, labelIsContent: true });
      });

      var before = [];
      if (headingKey && prompts.length > 1) {
        before.push(hint(ctx, headingKey));
      }
      if (minimum) {
        before.push(hint(ctx, "card.min_words", { count: minimum }));
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
      body: [content(ctx, "p", { className: "statement", text: component.content.statement }), group.node],
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
              children: [content(ctx, "span", { className: "text", text: item.text }), group.node],
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
          hint(ctx, "card.categorization_hint"),
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
      { legend: component.prompt, hideLegend: true, legendIsContent: true, grid: true }
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

  RENDERERS.scenario_choice = singleChoiceRenderer(function (component, ctx) {
    return {
      before: [content(ctx, "p", { className: "situation", text: component.content.situation })],
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
        content(ctx, "p", { className: "situation", text: component.content.situation }),
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
    var before = [content(ctx, "p", { className: "passage", text: component.content.background })];
    if (component.content.materials) {
      before.push(hint(ctx, "card.materials"));
      before.push(list(ctx, component.content.materials));
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
    var paragraph = content(ctx, "p", { className: "passage" });
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
    before: function (component, ctx) {
      return [content(ctx, "p", { className: "statement", text: component.content.cue })];
    },
  });
  RENDERERS.free_response = longTextRenderer({ rows: 8 });
  RENDERERS.rubric_response = longTextRenderer({
    rows: 8,
    before: function (component, ctx) {
      if (!component.content.requirements) {
        return [];
      }
      return [hint(ctx, "card.requirements"), list(ctx, component.content.requirements)];
    },
  });

  RENDERERS.translation = longTextRenderer({
    rows: 4,
    before: function (component, _ctx) {
      return [
        el("p", {
          className: "statement",
          text: component.content.source_text,
          // Tagged with the *source* locale rather than the exercise's content
          // locale: a translation card is the one place where those genuinely
          // differ, and the source text is the more specific claim.
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
      return [hint(ctx, "card.error_correction_hint")];
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
    var before = [hint(ctx, "card.code_notice", null, "notice")];
    if (component.content.requirements) {
      before.push(hint(ctx, "card.requirements"));
      before.push(list(ctx, component.content.requirements));
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

  RENDERERS.timeline = function (component, ctx) {
    // A date label is an ordering clue, so `show_dates: false` means it must not
    // be shown. The projection already withholds it in that case — this is the
    // second half of the same rule, so that a payload which somehow arrived
    // carrying one still would not display it.
    var showDates = component.content.show_dates === true;
    return orderingRenderer(
      "events",
      function (event) {
        return showDates && event.date_label
          ? event.date_label + " — " + event.text
          : event.text;
      },
      "card.timeline_hint"
    )(component, ctx);
  };

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
        content(ctx, "p", { className: "hint", text: component.content.start_stage_label }),
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
    // The back of the card is not in the payload and never will be: it lives in
    // the evaluator-only half. Turning the card over is therefore a *request*,
    // and one the server only grants after an attempt has been committed --
    // which is also the pedagogy. `ctx.reveal` is the application's authenticated
    // call; this renderer cannot reach the network itself.
    var revealed = false;
    var pending = false;

    var recall = answerField(ctx, {
      label: ctx.t("card.recall_label"),
      multiline: true,
      rows: 3,
    });
    var rating = choiceGroup(
      ctx,
      RATINGS.map(function (value) {
        return { value: value, label: ctx.t("rate." + value) };
      }),
      { legend: ctx.t("card.self_rate"), grid: true }
    );

    var backPanel = el("div", {
      className: "reveal",
      attrs: { "aria-live": "polite", id: uniqueId("reveal") },
    });
    var ratingPanel = el("div", { children: [rating.node] });
    ratingPanel.hidden = true;

    var problem = chrome(ctx, "p", { className: "error" });
    problem.hidden = true;

    var turn = chrome(ctx, "button", {
      className: "primary",
      text: ctx.t("card.turn_over"),
      attrs: { type: "button" },
    });

    function complain(message) {
      problem.textContent = message;
      problem.hidden = false;
    }

    turn.addEventListener("click", function () {
      if (revealed || pending) {
        return;
      }
      var attempt = recall.value().trim();
      if (!attempt) {
        // Enforced here for the message and on the server for the rule: a reveal
        // with no attempt behind it is reading, not recall.
        complain(ctx.t("invalid.attempt_required"));
        recall.focus();
        return;
      }
      problem.hidden = true;
      pending = true;
      turn.disabled = true;

      ctx.reveal(attempt).then(
        function (result) {
          pending = false;
          revealed = true;
          turn.hidden = true;
          // Frozen, because the server froze it. Leaving the field editable
          // would invite a learner to "improve" a recall the submission is then
          // refused for.
          recall.freeze(result.attempt);
          backPanel.replaceChildren(
            chrome(ctx, "p", { className: "field-label", text: ctx.t("card.back_label") }),
            content(ctx, "p", { className: "statement", text: result.back })
          );
          ratingPanel.hidden = false;
          rating.focus();
        },
        function (failure) {
          pending = false;
          turn.disabled = false;
          complain(failure && failure.message ? failure.message : ctx.t("server.body"));
        }
      );
    });

    var front = [content(ctx, "p", { className: "statement", text: component.content.front })];
    if (component.content.front_note) {
      front.push(content(ctx, "p", { className: "note", text: component.content.front_note }));
    }

    return {
      body: front.concat([recall.node, problem, turn, backPanel, ratingPanel]),
      focus: recall.focus,
      read: function () {
        if (!revealed) {
          return bad(ctx.t("invalid.reveal_first"));
        }
        var chosen = rating.selection();
        if (!chosen) {
          return bad(ctx.t("invalid.rating"));
        }
        // `text` is the frozen attempt, which is what the server will compare
        // the submission against.
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
      before.push(hint(ctx, "card.focus_points"));
      before.push(list(ctx, component.content.focus_points));
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
          ctx,
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

  //: How far one arrow key moves the crosshair, coarse and fine. Both appear in
  //: the card's own instructions, so a keyboard user is told rather than left to
  //: discover them.
  var HOTSPOT_COARSE_STEP = 0.05;
  var HOTSPOT_FINE_STEP = 0.01;

  //: Decimal places for a normalised coordinate. Four is finer than any screen
  //: and coarser than floating-point noise.
  var HOTSPOT_PRECISION = 4;

  function clamp01(value) {
    return Math.min(1, Math.max(0, value));
  }

  /**
   * Normalised-coordinate hotspot.
   *
   * The structure matters as much as the arithmetic, and the first version had
   * both wrong.
   *
   * **Only the picture is the control.** The button used to wrap the whole
   * `<figure>`: caption, long description, and the `<details>`/`<summary>` that
   * opens it. A `<summary>` inside a `<button>` is invalid HTML and unreachable
   * for a keyboard user, who can focus the button but can never open the
   * description nested inside it. The button now contains the picture and
   * nothing else, and every piece of prose is a sibling.
   *
   * **Coordinates come from the image, not from the control.** Measuring the
   * button measured the caption and the description too, so the same tap landed
   * on a different point depending on how much text the author had written --
   * adding a long description silently moved every answer. The rectangle used is
   * the `<img>`'s own. That is exact rather than approximate because the image is
   * sized `width: 100%; height: auto`: with no letterboxing, the element's box
   * and the picture's box are the same rectangle.
   *
   * The point is a fraction of the image's width and height, so it means the same
   * thing on any screen and at any zoom -- the stored answer's regions are in
   * those units too. Two ways in, one piece of state: tap the picture, or focus it
   * and use the arrow keys. A card that only accepted a tap could not honour
   * `keyboard_only`.
   */
  RENDERERS.hotspot = function (component, ctx) {
    var surface = null;
    var picture = figure(ctx, component.content.image, {
      onFallback: function (fallbackId) {
        if (surface) {
          surface.setAttribute(
            "aria-describedby",
            surface.getAttribute("aria-describedby") + " " + fallbackId
          );
        }
      },
    });
    var point = null;

    var marker = el("span", { className: "marker", attrs: { "aria-hidden": "true" } });
    marker.hidden = true;

    var overlay = [picture.holder, marker];
    if (component.content.show_grid) {
      overlay.push(el("span", { className: "hotspot-grid", attrs: { "aria-hidden": "true" } }));
    }

    var readoutId = uniqueId("hotspot-readout");
    var readout = chrome(ctx, "p", {
      className: "hint",
      attrs: { "aria-live": "polite", id: readoutId },
    });

    surface = el("button", {
      className: "hotspot",
      attrs: {
        type: "button",
        "aria-label": ctx.t("card.hotspot_pick"),
        // The readout is this control's value in words. The picture's text
        // alternative joins it only if the picture fails — see `onFallback`.
        "aria-describedby": readoutId,
      },
      children: overlay,
    });

    var clear = chrome(ctx, "button", {
      className: "small",
      text: ctx.t("card.hotspot_clear"),
      attrs: { type: "button" },
      props: { disabled: true },
    });

    function place(x, y) {
      point = { x: clamp01(x), y: clamp01(y) };
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
      // Measured on the image, so prose around it cannot move the coordinate.
      var box = picture.image.getBoundingClientRect
        ? picture.image.getBoundingClientRect()
        : null;
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
      var size = event.shiftKey ? HOTSPOT_FINE_STEP : HOTSPOT_COARSE_STEP;
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
      // Prose lives outside the control, where a keyboard user can reach it.
      body: [
        hint(ctx, "card.hotspot_hint"),
        hint(ctx, "card.hotspot_keyboard", {
          coarse: Math.round(HOTSPOT_COARSE_STEP * 100),
          fine: Math.round(HOTSPOT_FINE_STEP * 100),
        }),
        surface,
        readout,
        clear,
      ].concat(picture.extras),
      focus: function () {
        surface.focus();
      },
      read: function () {
        if (!point) {
          return bad(ctx.t("invalid.point"));
        }
        return ok({
          points: [
            {
              x: Number(point.x.toFixed(HOTSPOT_PRECISION)),
              y: Number(point.y.toFixed(HOTSPOT_PRECISION)),
            },
          ],
        });
      },
    };
  };

  RENDERERS.labeling = function (component, ctx) {
    var picture = figure(ctx, component.content.image);
    // The pins are positioned as a percentage of their containing box, so that
    // box has to be the picture and nothing else. Wrapping the whole figure --
    // caption, long description and all -- put every marker in the wrong place
    // by however tall the prose happened to be.
    var surface = el("div", { className: "hotspot", children: [picture.holder] });

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
      ].concat(picture.extras),
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
      prefilled[cell.row_id + " " + cell.column_id] = cell.text;
    });

    var head = el("tr", {
      children: [el("th", { attrs: { scope: "col" }, text: "" })].concat(
        component.content.columns.map(function (column) {
          return content(ctx, "th", { attrs: { scope: "col" }, text: column.header });
        })
      ),
    });

    var cells = [];
    var rows = component.content.rows.map(function (row) {
      var tds = component.content.columns.map(function (column) {
        var given = prefilled[row.id + " " + column.id];
        if (given !== undefined) {
          return content(ctx, "td", { text: given });
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
        children: [content(ctx, "th", { attrs: { scope: "row" }, text: row.header })].concat(tds),
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
        chrome(ctx, "h3", { text: ctx.t("card.unsupported.title") }),
        chrome(ctx, "p", { text: ctx.t("card.unsupported.body") }),
        hint(ctx, "card.unsupported.type", { type: component.type }),
      ],
      unsupported: true,
      read: function () {
        return bad(ctx.t("card.unsupported.title"));
      },
    };
  }

  /**
   * The badge at the top of a card: which kind of card this is.
   *
   * The icon is chosen by *component type* — the application's own
   * discriminator, never anything out of a payload — so a card cannot be made
   * to draw something an author picked. It is `aria-hidden`, because it is a
   * second way of saying what the label beside it already says.
   *
   * The label is `card.type.<type>` when the interface has a name for this kind
   * of card, and nothing at all when it does not: a type this build has never
   * heard of gets the generic icon rather than a dotted key printed at a
   * learner.
   */
  function typeBadge(component, ctx) {
    var drawing = Icons ? Icons.icon(Icons.iconFor(component.type)) : null;
    if (!drawing) {
      return null;
    }
    var children = [drawing];
    var key = "card.type." + component.type;
    var label = ctx.t(key);
    if (label && label !== key) {
      children.push(chrome(ctx, "span", { className: "type-label", text: label }));
    }
    return el("div", { className: "type-badge", children: children });
  }

  /** The accessibility metadata that travels with a component, made visible. */
  function alternatives(component, ctx) {
    var access = component.accessibility || {};
    var nodes = [];
    if (access.caption) {
      nodes.push(content(ctx, "p", { className: "note", text: access.caption }));
    }
    if (access.keyboard_alternative) {
      nodes.push(
        hint(ctx, "card.keyboard_alternative", { text: access.keyboard_alternative })
      );
    }
    if (access.long_description) {
      nodes.push(
        el("details", {
          children: [
            chrome(ctx, "summary", { text: ctx.t("card.long_description") }),
            content(ctx, "p", { className: "note", text: access.long_description }),
          ],
        })
      );
    }
    if (access.transcript) {
      nodes.push(
        el("details", {
          children: [
            chrome(ctx, "summary", { text: ctx.t("card.transcript") }),
            content(ctx, "p", { className: "note", text: access.transcript }),
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
      children: [
        typeBadge(view, ctx),
        content(ctx, "h2", { className: "prompt", text: view.prompt }),
      ]
        .concat(alternatives(view, ctx))
        .concat(built.body),
    });

    // The card is the learner-content container, so it carries the exercise's
    // own language. Interface strings inside it are marked back to the UI
    // language by `chrome()`; everything outside it -- the title bar, the
    // progress text, the action button -- is chrome and inherits the document's.
    var locale = contentLang(ctx);
    if (locale) {
      section.setAttribute("lang", locale);
    }

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
