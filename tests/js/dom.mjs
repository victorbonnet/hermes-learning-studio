/*
 * A DOM small enough to read, for testing the frontend without a browser.
 *
 * Why this exists rather than jsdom or Playwright: the plugin's dependency
 * story is that `pip install` pulls in nothing, and `uv sync --extra dev`
 * installs seven Python packages. Adding a Node package manager, a lockfile,
 * and a browser download to that in order to check that a radio group produces
 * `{option_id}` would be the largest dependency in the repository by two orders
 * of magnitude, and CI would then be the thing most likely to be broken.
 *
 * What this buys, and what it does not: the renderers are *executed*, so their
 * dispatch, their validation, their response shapes, and the fact that nothing
 * they build carries a hidden field are all really tested. Layout, paint,
 * native radio grouping, focus order, and Content-Security-Policy enforcement
 * are not simulated at all -- those are checked by the contract tests in
 * `tests/test_mini_app_frontend.py`, by the header tests in
 * `tests/test_mini_app_ui.py`, and by looking at the thing on a phone.
 *
 * The frontend is written to make this shim viable: selection state lives in
 * closures rather than being read back out of the tree, and no renderer uses a
 * CSS selector. That is not an accident of testing -- state you keep is state
 * you can reason about -- but it is worth naming, because a renderer that
 * started reading `input.checked` back would pass here and be wrong on a phone.
 */

const VOID_TAGS = new Set(["input", "img", "br", "hr", "meta", "link"]);

class ClassList {
  constructor(node) {
    this.node = node;
  }

  _values() {
    return String(this.node.className || "")
      .split(/\s+/)
      .filter(Boolean);
  }

  add(name) {
    const values = this._values();
    if (!values.includes(name)) {
      values.push(name);
      this.node.className = values.join(" ");
    }
  }

  remove(name) {
    this.node.className = this._values()
      .filter((value) => value !== name)
      .join(" ");
  }

  contains(name) {
    return this._values().includes(name);
  }
}

class Style {
  setProperty(name, value) {
    this[name] = String(value);
  }

  removeProperty(name) {
    const previous = this.getPropertyValue(name);
    delete this[name];
    return previous;
  }

  getPropertyValue(name) {
    return this[name] === undefined ? "" : this[name];
  }
}

class Element {
  /**
   * `namespaceURI` is set for anything built with `createElementNS` — the icon
   * set and the score ring. Its tag name keeps the case it was given, because
   * SVG has camel-cased elements and a shim that lower-cased them would make a
   * real mistake pass here.
   */
  constructor(tagName, ownerDocument, namespaceURI = null) {
    this.namespaceURI = namespaceURI;
    this.tagName = namespaceURI ? String(tagName) : String(tagName).toLowerCase();
    this.ownerDocument = ownerDocument;
    this.attributes = {};
    this.children = [];
    this.parentNode = null;
    this.listeners = {};
    this.style = new Style();
    this.classList = new ClassList(this);
    this.className = "";
    this.hidden = false;
    this.disabled = false;
    this.onclick = null;
    this._text = "";
    if (VOID_TAGS.has(this.tagName) || this.tagName === "textarea" || this.tagName === "select") {
      this.value = "";
      this.checked = false;
    }
  }

  get id() {
    return this.attributes.id || "";
  }

  set id(value) {
    this.setAttribute("id", value);
  }

  /**
   * Concatenated descendant text, as a browser reports it.
   *
   * Getting this right is what makes "no hidden field reached the card" a real
   * assertion rather than a check on one element. Note that an input's `value`
   * is deliberately *not* included, which is also what a browser does.
   */
  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join("");
  }

  set textContent(value) {
    this.children = [];
    this._text = value === null || value === undefined ? "" : String(value);
  }

  setAttribute(name, value) {
    this.attributes[String(name)] = String(value);
  }

  getAttribute(name) {
    const value = this.attributes[String(name)];
    return value === undefined ? null : value;
  }

  hasAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, String(name));
  }

  removeAttribute(name) {
    delete this.attributes[String(name)];
  }

  appendChild(child) {
    if (child.parentNode) {
      child.parentNode.removeChild(child);
    }
    child.parentNode = this;
    this.children.push(child);
    if (child.id) {
      this.ownerDocument._register(child);
    }
    child._walk((node) => {
      if (node.id) {
        this.ownerDocument._register(node);
      }
    });
    return child;
  }

  removeChild(child) {
    this.children = this.children.filter((node) => node !== child);
    child.parentNode = null;
    return child;
  }

  remove() {
    if (this.parentNode) {
      this.parentNode.removeChild(this);
    }
  }

  replaceChildren(...nodes) {
    this.children.forEach((child) => {
      child.parentNode = null;
    });
    this.children = [];
    this._text = "";
    nodes.forEach((node) => this.appendChild(node));
  }

  addEventListener(type, handler) {
    (this.listeners[type] = this.listeners[type] || []).push(handler);
  }

  removeEventListener(type, handler) {
    this.listeners[type] = (this.listeners[type] || []).filter((entry) => entry !== handler);
  }

  dispatchEvent(event) {
    const payload = Object.assign({ target: this, preventDefault() {} }, event);
    (this.listeners[payload.type] || []).forEach((handler) => handler(payload));
    if (payload.type === "click" && typeof this.onclick === "function") {
      this.onclick(payload);
    }
    return true;
  }

  focus() {
    this.ownerDocument.activeElement = this;
  }

  /** Zeroes, so a pointer-driven code path cannot silently be the tested one. */
  getBoundingClientRect() {
    return { width: 0, height: 0, left: 0, top: 0, right: 0, bottom: 0 };
  }

  _walk(visit) {
    this.children.forEach((child) => {
      visit(child);
      child._walk(visit);
    });
  }

  /** Every descendant, self included. */
  all() {
    const found = [this];
    this._walk((node) => found.push(node));
    return found;
  }

  /** Descendants of a tag, for tests only — the frontend uses no selectors. */
  byTag(tagName) {
    return this.all().filter((node) => node.tagName === String(tagName).toLowerCase());
  }

  /**
   * Markup-ish text including attributes and input values.
   *
   * Used by the "nothing hidden reached the learner" assertions, which have to
   * look in more places than `textContent` does: a leak into an `aria-label` or
   * a prefilled value would be just as much of a leak.
   */
  serialize() {
    const attrs = Object.keys(this.attributes)
      .sort()
      .map((name) => ` ${name}="${this.attributes[name]}"`)
      .join("");
    const value = this.value ? ` value="${this.value}"` : "";
    const inner = this._text + this.children.map((child) => child.serialize()).join("");
    return `<${this.tagName}${attrs}${value}>${inner}</${this.tagName}>`;
  }
}

class FakeDocument {
  constructor() {
    this._byId = new Map();
    this.documentElement = new Element("html", this);
    this.body = new Element("body", this);
    this.documentElement.appendChild(this.body);
    this.activeElement = null;
    this.readyState = "complete";
  }

  createElement(tagName) {
    return new Element(tagName, this);
  }

  createElementNS(namespaceURI, tagName) {
    return new Element(tagName, this, String(namespaceURI));
  }

  getElementById(id) {
    return this._byId.get(String(id)) || null;
  }

  _register(node) {
    this._byId.set(node.id, node);
  }
}

/** A window-ish global with the handful of APIs the frontend touches. */
export function createWindow(overrides = {}) {
  const document = new FakeDocument();
  const revoked = [];
  const created = [];

  const win = {
    document,
    // The launch selector arrives in the fragment, so the shim has to have
    // one -- and a `history` for the page to strip it with.
    location: { search: "", hash: "", pathname: "/" },
    history: {
      replaced: [],
      replaceState(state, title, url) {
        this.replaced.push(url);
      },
    },
    URLSearchParams,
    URL: {
      createObjectURL(blob) {
        const url = `blob:fake/${created.length}`;
        created.push({ url, blob });
        return url;
      },
      revokeObjectURL(url) {
        revoked.push(url);
      },
    },
    console,
    setTimeout,
    clearTimeout,
    objectUrls: { created, revoked },
  };
  Object.assign(win, overrides);
  win.window = win;
  return win;
}

/** Fire a click the way a person would: through the element's listeners. */
export function click(node) {
  node.dispatchEvent({ type: "click" });
}

/** Check a radio or checkbox and let the renderer's handler see it. */
export function check(input, checked = true) {
  input.checked = checked;
  input.dispatchEvent({ type: "change" });
}

/** Type into a field and fire the event a real one would. */
export function type(node, value) {
  node.value = String(value);
  node.dispatchEvent({ type: "input" });
}

/** Press a key on a focusable element. */
export function press(node, key, modifiers = {}) {
  node.dispatchEvent(Object.assign({ type: "keydown", key }, modifiers));
}

export { Element, FakeDocument };
