/*
 * The Mini App: launch, session, cards, states.
 *
 * What this file is responsible for, and the reasoning behind each part:
 *
 * **It is the only thing that talks to the server.** `initData` goes into one
 * request header, on same-origin paths written as literals in this file. It is
 * never put in a URL, a query string, a cookie, or `localStorage`: a query
 * string ends up in logs and referrers, and a cookie would be attached to
 * requests this page did not make. Nothing is persisted between launches at
 * all, so there is no stored credential to steal.
 *
 * **It is the only thing that turns an asset reference into a URL.** Renderers
 * are handed a `loadImage(assetRef)` and cannot reach the network themselves.
 * The reference is opaque, the path is built here, and the server checks that
 * this session's own experience actually mentions the asset. A URL out of a
 * manifest would never arrive here -- the registry has no field to carry one --
 * and if one did, it would still not be fetched, because nothing here reads an
 * address out of a payload.
 *
 * **It never renders a server message.** Every visible string comes from
 * `i18n.js`, chosen by HTTP status. The API's own English error text is
 * deliberately dropped: it is written for an operator reading a log, not for a
 * learner, and translating the interface only to fall back to English on the
 * unhappy path would be a strange kind of half-localized.
 *
 * **Every failure has a state.** Loading, wrong launch context, failed
 * verification, unauthorised account, expired session, missing exercise,
 * conflict, rate limit, server error, and offline each render something a
 * person can act on. The alternative -- a spinner that never resolves -- is the
 * usual outcome of treating the happy path as the only one.
 */

(function (global) {
  "use strict";

  var I18n = global.LearningStudioI18n;
  var Renderers = global.LearningStudioRenderers;

  var INIT_DATA_HEADER = "X-Telegram-Init-Data";
  var SESSION_HEADER = "X-Learning-Studio-Session";

  var API = {
    session: "/api/session",
    component: "/api/session/component",
    answer: "/api/session/answer",
    reveal: "/api/session/reveal",
    result: "/api/session/result",
    summary: "/api/session/summary",
    assets: "/api/assets/",
  };

  //: An experience identifier is an opaque server-generated token. Checking its
  //: shape before putting it in a request body is not authorisation -- the
  //: server owns that -- it just turns a mangled deep link into an honest
  //: "not found" card instead of a pointless round trip.
  // The launch selector, in exactly the shape the runtime issues. Anchored, so
  // a fragment carrying anything else is not sent at all.
  var LAUNCH_ID = /^[A-Za-z0-9_-]{16,64}$/;
  var LAUNCH_FRAGMENT_KEY = "launch";

  //: Telegram theme values are colours, and this is what a CSS hex colour may
  //: look like: exactly 3, 4, 6, or 8 digits. Custom properties accept almost
  //: any token sequence, so an unchecked value from the host client would be a
  //: way to write arbitrary CSS into the page; a rejected value falls through to
  //: the stylesheet's own fallback, which is legible by construction.
  //
  //: `{3,8}` was wrong, not merely loose. It accepted `#12345` and `#1234567`,
  //: which are not colours at all. A custom property accepts them anyway — it is
  //: only checked where it is *used* — so the property ends up set to an invalid
  //: value rather than left unset, and `var(--tg-…, fallback)` never gets its
  //: chance. Measured in Chromium: `--tg-bg-color: #12345` computes the page
  //: background to `rgba(0, 0, 0, 0)`, fully transparent, against which the
  //: stylesheet's own foreground colours are anybody's guess. Removing the
  //: property instead computes the intended `rgb(255, 255, 255)`.
  var COLOUR = /^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/;

  var THEME_KEYS = [
    "bg_color",
    "text_color",
    "hint_color",
    "link_color",
    "button_color",
    "button_text_color",
    "secondary_bg_color",
  ];

  function createApp(settings) {
    var options = settings || {};
    var doc = options.document;
    var telegram = options.telegram || null;
    var fetchImpl = options.fetch || null;
    var locationRef = options.location || null;

    var t = I18n.translator(I18n.FALLBACK_LOCALE);
    var sessionToken = "";
    // Read from the URL fragment once and kept, because the fragment is
    // removed from history immediately afterwards and a restart still needs to
    // know which launch this page is.
    var launchId = "";
    var experience = null;
    var progress = null;
    var currentCard = null;
    var currentComponent = null;
    var objectUrls = [];
    //: Told to us by the bootstrap response. Defaults are the shipped
    //: configuration's, so a request made before the session opens is still
    //: bounded, but the server's answer is what governs.
    var limits = { max_response_chars: 4000, max_request_bytes: 16384 };

    var nodes = {};
    [
      "skip-link",
      "top-bar",
      "exercise-title",
      "exercise-meta",
      "progress",
      "progress-fill",
      "progress-text",
      "card",
      "announcer",
      "actions",
      "field-error",
      "primary-action",
    ].forEach(function (id) {
      nodes[id] = doc.getElementById(id);
    });

    var el = Renderers.el;

    // ── Chrome ──────────────────────────────────────────────────────────

    function announce(message) {
      nodes.announcer.textContent = message || "";
    }

    function showFieldError(message) {
      nodes["field-error"].textContent = message || "";
      nodes["field-error"].hidden = !message;
      if (message) {
        announce(message);
      }
    }

    function setAction(label, handler) {
      var button = nodes["primary-action"];
      button.textContent = label || "";
      nodes.actions.hidden = !label;
      button.hidden = !label;
      button.disabled = false;
      button.onclick = handler || null;
    }

    function applyLocale() {
      t = I18n.translator(experience ? experience.ui_locale : I18n.FALLBACK_LOCALE);
      if (doc.documentElement) {
        // The document language is the *interface* language. Exercise content
        // carries its own `lang`, applied to the card by the renderers.
        doc.documentElement.setAttribute("lang", t.locale);
      }
      // The tab and the task switcher show this, and an untranslated title in an
      // otherwise translated app is the kind of detail that reads as unfinished.
      doc.title = t("app.title");
      nodes["skip-link"].textContent = t("app.skip");
      nodes.progress.setAttribute("aria-label", t("progress.label"));
    }

    function applyTheme() {
      if (!telegram || !doc.documentElement) {
        return;
      }
      var root = doc.documentElement;
      root.setAttribute("data-theme", telegram.colorScheme === "dark" ? "dark" : "light");
      var params = telegram.themeParams || {};
      if (!root.style) {
        return;
      }
      THEME_KEYS.forEach(function (key) {
        var value = params[key];
        var property = "--tg-" + key.replace(/_/g, "-");
        if (typeof value === "string" && COLOUR.test(value)) {
          root.style.setProperty(property, value);
        } else {
          // Removed rather than left alone. `themeChanged` sends a whole palette,
          // and a key that is absent this time — or arrives malformed — means the
          // client no longer has a colour for it. Keeping the previous value
          // would paint a light-theme background under dark-theme text, which is
          // exactly the case a switch between themes produces. Removing the
          // property hands the decision back to the stylesheet, whose fallbacks
          // are contrast-checked.
          root.style.removeProperty(property);
        }
      });
    }

    function showExperience() {
      if (!experience) {
        return;
      }
      nodes["top-bar"].hidden = false;
      nodes["exercise-title"].textContent = experience.title;
      nodes["exercise-meta"].textContent = t("meta.summary", {
        minutes: experience.expected_duration_minutes,
        difficulty: t("difficulty." + experience.difficulty),
        count: experience.component_count,
      });
    }

    function showProgress() {
      if (!progress) {
        return;
      }
      var total = progress.component_count || 1;
      var position = Math.min(progress.position + 1, total);
      var fraction = Math.min(1, progress.position / total);
      nodes["progress-fill"].style.width = Math.round(fraction * 100) + "%";
      nodes.progress.setAttribute("aria-valuemax", String(total));
      nodes.progress.setAttribute("aria-valuenow", String(progress.position));
      nodes["progress-text"].textContent = progress.completed
        ? ""
        : t("progress.text", { position: position, count: total });
    }

    /**
     * The size a request body will actually be, in bytes rather than characters.
     *
     * The server's ceiling is on bytes, and the difference matters: a card
     * answered in Japanese or with an emoji in it costs three or four bytes per
     * character, so a `length` check would pass a body the server then refuses.
     */
    function byteLength(text) {
      if (global.TextEncoder) {
        return new global.TextEncoder().encode(text).length;
      }
      // Counted directly rather than via `unescape(encodeURIComponent(...))`:
      // that idiom works but relies on a deprecated global, and this is a dozen
      // arithmetic operations on a string we already hold.
      var bytes = 0;
      for (var index = 0; index < text.length; index += 1) {
        var code = text.charCodeAt(index);
        if (code < 0x80) {
          bytes += 1;
        } else if (code < 0x800) {
          bytes += 2;
        } else if (code >= 0xd800 && code <= 0xdbff) {
          // A surrogate pair is one four-byte code point; skip its low half.
          bytes += 4;
          index += 1;
        } else {
          bytes += 3;
        }
      }
      return bytes;
    }

    function releaseImages() {
      if (global.URL && global.URL.revokeObjectURL) {
        objectUrls.forEach(function (url) {
          global.URL.revokeObjectURL(url);
        });
      }
      objectUrls = [];
    }

    /** Replace the card, move focus to it, and clear anything stale. */
    function paint(element, options) {
      var config = options || {};
      releaseImages();
      showFieldError("");
      nodes.card.replaceChildren(element);
      if (config.focus) {
        config.focus();
      } else if (nodes.card.focus) {
        nodes.card.focus();
      }
    }

    /**
     * One state card: a title, a sentence, and whatever else it needs.
     *
     * ``values`` interpolates the sentence, because the completion state's body
     * counts what was answered. Passing them here rather than appending a second
     * paragraph is not a style choice — doing it the other way showed the body
     * twice, once with ``{answered}`` and ``{count}`` still in it.
     */
    function state(name, extra, values) {
      // Loading is a title and a spinner: there is nothing useful to say about
      // a wait, and a sentence explaining it would only be read once.
      var children =
        name === "loading"
          ? [
              el("div", { className: "spinner", attrs: { "aria-hidden": "true" } }),
              el("h2", { text: t("loading.title") }),
            ]
          : [
              el("h2", { text: t(name + ".title") }),
              el("p", { text: t(name + ".body", values) }),
            ];
      (extra || []).forEach(function (node) {
        children.push(node);
      });
      return el("section", {
        className: "state",
        attrs: { "data-state": name },
        children: children,
      });
    }

    function showState(name, action) {
      paint(state(name));
      announce(t(name + ".title"));
      if (action) {
        setAction(t(action.label), action.handler);
      } else {
        setAction("", null);
      }
    }

    // ── Transport ───────────────────────────────────────────────────────

    function headers(withSession) {
      var result = {};
      result[INIT_DATA_HEADER] = telegram && telegram.initData ? telegram.initData : "";
      if (withSession && sessionToken) {
        result[SESSION_HEADER] = sessionToken;
      }
      return result;
    }

    /**
     * One request. Resolves `{ok, status, data}` or `{ok:false, offline:true}`.
     *
     * A rejected `fetch` and an HTTP error are different things to a reader --
     * "no connection" versus "the server said no" -- so they stay separate all
     * the way to the state that gets rendered.
     */
    function request(method, path, body, withSession) {
      var init = {
        method: method,
        headers: headers(withSession !== false),
        // No cookies, ever: this API authenticates by header and a credentialed
        // request would only add ambient state it ignores.
        credentials: "omit",
        cache: "no-store",
        referrerPolicy: "no-referrer",
      };
      if (body !== undefined && body !== null) {
        init.headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(body);
      }
      return fetchImpl(path, init).then(
        function (response) {
          return response
            .json()
            .catch(function () {
              return {};
            })
            .then(function (data) {
              return { ok: response.ok, status: response.status, data: data || {} };
            });
        },
        function () {
          return { ok: false, offline: true, status: 0, data: {} };
        }
      );
    }

    /** Map a failed request onto the state that describes it. */
    function fail(result, phase) {
      if (result.offline) {
        return showState("offline", { label: "action.retry", handler: restart });
      }
      if (result.status === 401) {
        return phase === "bootstrap"
          ? showState("auth")
          : showState("expired", { label: "action.reopen", handler: restart });
      }
      if (result.status === 403) {
        return showState("forbidden");
      }
      if (result.status === 404) {
        return showState("notfound");
      }
      if (result.status === 409) {
        return showState("conflict", { label: "action.reopen", handler: restart });
      }
      if (result.status === 429) {
        return showState("throttled", { label: "action.retry", handler: restart });
      }
      return showState("server", { label: "action.retry", handler: restart });
    }

    /**
     * Fetch one managed image and hand back a blob URL.
     *
     * `<img src="/api/assets/…">` cannot work here: the route needs two request
     * headers and a navigation-initiated image load sends neither. So the bytes
     * are fetched like anything else and shown from a blob URL, which names only
     * data this page already holds. The content type is checked because a route
     * that is expected to return an image is not a route worth trusting to.
     */
    function loadImage(assetRef) {
      return fetchImpl(API.assets + encodeURIComponent(String(assetRef)), {
        method: "GET",
        headers: headers(true),
        credentials: "omit",
        cache: "no-store",
      })
        .then(function (response) {
          if (!response.ok) {
            throw new Error("asset");
          }
          return response.blob();
        })
        .then(function (blob) {
          if (!blob || String(blob.type || "").indexOf("image/") !== 0) {
            throw new Error("asset");
          }
          var url = global.URL.createObjectURL(blob);
          objectUrls.push(url);
          return url;
        });
    }

    /**
     * Ask the server to turn the current card over.
     *
     * Rejects with an already-localized message, because the renderer shows it
     * and the API's own English is written for an operator. The attempt goes in
     * the body; the server freezes it and answers with the frozen value, which is
     * what the card then displays and submits.
     */
    function reveal(attempt) {
      return request("POST", API.reveal, {
        component_id: currentComponent.component_id,
        attempt: attempt,
      }).then(function (result) {
        if (result.ok) {
          return { back: result.data.back, attempt: result.data.attempt };
        }
        if (result.offline) {
          throw { message: t("offline.body") };
        }
        if (result.status === 400) {
          throw { message: t("invalid.attempt_required") };
        }
        if (result.status === 401) {
          // The session went away mid-card; the state machine owns that.
          fail(result, "session");
          throw { message: t("expired.body") };
        }
        throw { message: t("server.body") };
      });
    }

    function renderContext() {
      return {
        t: function (key, values) {
          return t(key, values);
        },
        loadImage: loadImage,
        reveal: reveal,
        uiLocale: t.locale,
        // The exercise's own language, which is not the interface's. Absent until
        // a session is open, which is also when the first card can be rendered.
        contentLocale: experience ? experience.content_locale : null,
      };
    }

    // ── Flow ────────────────────────────────────────────────────────────

    /**
     * Which launch to open.
     *
     * The button's URL carries `#launch=<launch id>`. A fragment is read by
     * the page and never sent to the server, so the selector appears in no
     * request line, no access log and no `Referer` -- and it is removed from
     * the visible history as soon as it has been read, so a screenshot or a
     * shared history entry does not carry it either.
     *
     * It is a *selector*, not a capability: it says which launch is meant, and
     * the server still requires verified `initData`, an allowlisted account
     * that owns the exercise, and a grant issued to that same account. So
     * sending one that is not ours is refused rather than dangerous, and there
     * is nothing here that has to be kept secret.
     *
     * `start_param` is deliberately not consulted. An inline `web_app` button
     * does not produce one -- that is a property of `t.me` deep links -- so
     * relying on it meant the page had nothing to open at all.
     */
    function resolveLaunchId() {
      var raw = "";
      if (locationRef && typeof locationRef.hash === "string") {
        raw = locationRef.hash;
      }
      var fragment = raw.charAt(0) === "#" ? raw.slice(1) : raw;
      var found = "";
      var parts = fragment.split("&");
      for (var index = 0; index < parts.length; index += 1) {
        var pair = parts[index].split("=");
        if (pair[0] === LAUNCH_FRAGMENT_KEY && pair.length > 1) {
          found = pair.slice(1).join("=");
          break;
        }
      }
      if (!LAUNCH_ID.test(found)) {
        return "";
      }
      forgetFragment();
      return found;
    }

    /**
     * Drop the selector from the address bar and from history.
     *
     * Best effort by design: an environment without `history.replaceState` is
     * not a reason to refuse to open the exercise, because the selector is not
     * a secret. It is removed because there is no reason to leave it lying
     * around, not because leaving it would be unsafe.
     */
    function forgetFragment() {
      try {
        if (global.history && typeof global.history.replaceState === "function") {
          var path = (locationRef && locationRef.pathname) || "/";
          var search = (locationRef && locationRef.search) || "";
          global.history.replaceState(null, "", path + search);
        }
      } catch (error) {
        // An environment that refuses the rewrite changes nothing about safety.
      }
    }

    function restart() {
      sessionToken = "";
      return openSession();
    }

    function openSession() {
      // Read once, at the first open. A restart must not depend on the
      // fragment still being there -- it has been removed from history by
      // then -- so the selector is remembered for the life of the page.
      if (!launchId) {
        launchId = resolveLaunchId();
      }
      showState("loading");
      if (!launchId) {
        return Promise.resolve(showState("notfound"));
      }
      return request("POST", API.session, { launch_id: launchId }, false).then(
        function (result) {
          if (!result.ok) {
            return fail(result, "bootstrap");
          }
          sessionToken = result.data.session_token || "";
          experience = result.data.experience || null;
          if (result.data.limits) {
            limits = result.data.limits;
          }
          progress = result.data.progress || null;
          applyLocale();
          showExperience();
          showProgress();
          return fetchComponent();
        }
      );
    }

    function fetchComponent() {
      return request("GET", API.component).then(function (result) {
        if (!result.ok) {
          return fail(result, "session");
        }
        progress = result.data.progress || progress;
        return showComponent(result.data.component);
      });
    }

    function showComponent(component) {
      showProgress();
      if (!component) {
        return showResult();
      }
      currentComponent = component;
      currentCard = Renderers.render(component, renderContext());
      paint(currentCard.element, { focus: currentCard.focus });
      if (currentCard.unsupported) {
        // Nothing to submit, but the exercise should not dead-end: skipping the
        // card is the only honest move available.
        setAction(t("action.continue"), function () {
          return submit({ skipped: true });
        });
      } else {
        setAction(t("action.submit"), function () {
          return submit();
        });
      }
      return Promise.resolve();
    }

    function submit(override) {
      var payload = override;
      if (!payload) {
        var read = currentCard.read();
        if (!read.ok) {
          showFieldError(read.error);
          if (currentCard.focus) {
            currentCard.focus();
          }
          return Promise.resolve();
        }
        payload = read.response;
      }

      // The per-field limit is enforced by each textarea's `maxlength`, but a
      // card with eight prompts can clear every field limit and still exceed the
      // request ceiling. Left unchecked that arrives as a 413 rendered as "the
      // server said no", which is both wrong and unactionable; caught here it is
      // a sentence telling the learner to shorten what they wrote.
      var body = JSON.stringify({
        component_id: currentComponent.component_id,
        response: payload,
      });
      if (byteLength(body) > limits.max_request_bytes) {
        showFieldError(t("invalid.too_long"));
        return Promise.resolve();
      }

      showFieldError("");
      nodes["primary-action"].disabled = true;

      return request("POST", API.answer, {
        component_id: currentComponent.component_id,
        response: payload,
      }).then(function (result) {
        nodes["primary-action"].disabled = false;
        if (!result.ok) {
          return fail(result, "session");
        }
        progress = result.data.progress || progress;
        showProgress();
        return acknowledge(result.data.next_component);
      });
    }

    /**
     * Confirm the answer was recorded, and say plainly that it is not marked
     * yet.
     *
     * Scoring happens once for the whole session, on the completion screen --
     * not here, per answer -- so a tick or a cross on this card would be
     * guessing ahead of the mark that ``GET /api/session/summary`` is what
     * actually produces. The next card is one deliberate tap away.
     */
    function acknowledge(nextComponent) {
      var message = el("section", {
        className: "state",
        attrs: { "data-state": "recorded" },
        children: [
          el("p", { className: "feedback", text: t("feedback.recorded") }),
          el("p", { className: "hint", text: t("feedback.pending") }),
        ],
      });
      paint(message);
      announce(t("feedback.recorded") + " " + t("feedback.pending"));
      setAction(nextComponent ? t("action.continue") : t("action.finish"), function () {
        return showComponent(nextComponent);
      });
      return Promise.resolve();
    }

    /**
     * One row of the per-component breakdown.
     *
     * ``feedback`` is the one piece of evaluator-only text the server ever
     * discloses, and only for *this* learner's *own* completed attempt -- see
     * ``learning_studio.evaluation``. Nothing here shows an answer key or a
     * rubric; a component with no mark at all (a self-report, or a rubric
     * response nobody has graded) says only that it was recorded.
     */
    function summaryItem(component) {
      var outcome =
        component.graded === true
          ? component.correct
            ? "correct"
            : "incorrect"
          : "ungraded";
      var label =
        outcome === "correct"
          ? t("complete.correct")
          : outcome === "incorrect"
            ? t("complete.incorrect")
            : t("complete.recorded_item");
      var children = [el("p", { className: "outcome", text: label })];
      if (component.feedback) {
        children.push(el("p", { className: "detail", text: String(component.feedback) }));
      }
      return el("li", {
        className: "summary-item",
        attrs: { "data-outcome": outcome },
        children: children,
      });
    }

    /** The completion screen: what was scored, what was not, what is next. */
    function renderCompletion(data) {
      var total = data.component_count || 0;
      var graded = data.graded_component_count || 0;
      var ungraded = Math.max(0, total - graded);

      var body = [el("p", { text: t("complete.body", { answered: total, count: total }) })];
      if (graded > 0) {
        body.push(
          el("p", {
            className: "feedback",
            text: t("complete.score", { correct: data.correct_component_count || 0, graded: graded }),
          })
        );
      } else {
        body.push(el("p", { className: "hint", text: t("complete.no_score") }));
      }
      if (ungraded > 0 && graded > 0) {
        body.push(el("p", { className: "hint", text: t("complete.ungraded", { count: ungraded }) }));
      }

      var components = data.components || [];
      if (components.length) {
        body.push(
          el("ul", {
            className: "summary-list",
            attrs: { "aria-label": t("complete.title") },
            children: components.map(summaryItem),
          })
        );
      }

      if (data.review && data.review.next_review_at) {
        body.push(
          el("p", {
            className: "hint",
            text: t("complete.review", { date: String(data.review.next_review_at).slice(0, 10) }),
          })
        );
      }
      body.push(el("p", { className: "hint", text: t("complete.guidance") }));

      paint(
        el("section", {
          className: "state",
          attrs: { "data-state": "complete" },
          children: [el("h2", { text: t("complete.title") })].concat(body),
        })
      );
      announce(t("complete.title"));
      nodes["progress-text"].textContent = "";
      setAction(t("action.close"), function () {
        if (telegram && typeof telegram.close === "function") {
          telegram.close();
        }
      });
    }

    function showResult() {
      return request("GET", API.summary).then(function (result) {
        if (!result.ok) {
          return fail(result, "session");
        }
        renderCompletion(result.data);
        return undefined;
      });
    }

    /**
     * Boot.
     *
     * The launch context is checked first and refused loudly: a webview opened
     * outside Telegram has no `initData`, every request would answer 401, and
     * "Telegram could not verify this launch" would be a misleading way to say
     * "you opened this in the wrong place".
     */
    function start() {
      applyLocale();

      if (!telegram || typeof telegram.initData !== "string" || !telegram.initData) {
        showState("unsupported");
        return Promise.resolve();
      }
      if (typeof telegram.ready === "function") {
        telegram.ready();
      }
      if (typeof telegram.expand === "function") {
        telegram.expand();
      }
      if (typeof telegram.enableClosingConfirmation === "function") {
        // Half an exercise lost to a stray swipe is a bad way to learn anything.
        telegram.enableClosingConfirmation();
      }
      applyTheme();
      if (typeof telegram.onEvent === "function") {
        telegram.onEvent("themeChanged", applyTheme);
      }
      return openSession();
    }

    return {
      start: start,
      /** Exposed for tests and for nothing else: no caller may set them. */
      inspect: function () {
        return {
          locale: t.locale,
          sessionOpen: Boolean(sessionToken),
          progress: progress,
          component: currentComponent,
          card: currentCard,
        };
      },
    };
  }

  function defaults() {
    var webApp = global.Telegram && global.Telegram.WebApp ? global.Telegram.WebApp : null;
    return {
      document: global.document,
      telegram: webApp,
      fetch: typeof global.fetch === "function" ? global.fetch.bind(global) : null,
      location: global.location || null,
    };
  }

  global.LearningStudioApp = { createApp: createApp };

  // Deferred scripts run after the document is parsed, so every element the
  // application needs already exists and there is nothing to wait for.
  if (global.document && global.document.getElementById("card")) {
    var app = createApp(defaults());
    global.LearningStudioApp.instance = app;
    global.LearningStudioApp.booted = app.start();
  }
})(typeof window !== "undefined" ? window : this);
