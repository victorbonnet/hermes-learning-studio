/* Serve the repository root; open tests/browser/ordering-focus.html.
 * This fixture needs no test framework, package install, API or credentials.
 * DOM clicks exercise the keyboard button handlers without automation that
 * would scroll every target into view and conceal a focus regression.
 */
(function () {
  "use strict";

  var main = document.getElementById("card");
  var report = document.getElementById("browser-results");
  var instructions = document.getElementById("pointer-instructions");
  var currentCard = null;
  var pointerCheck = null;
  var results = { checks: [], pointer: { status: "pending" } };
  window.orderingFocusResults = results;

  function assert(condition, message) {
    if (!condition) { throw new Error(message); }
  }

  function viewport() {
    var visual = window.visualViewport;
    var top = visual ? visual.offsetTop : 0;
    var bottom = top + (visual ? visual.height : window.innerHeight);
    var footer = document.getElementById("actions").getBoundingClientRect();
    if (footer.bottom > top && footer.top < bottom) { bottom = footer.top; }
    top += parseFloat(window.getComputedStyle(document.body).paddingTop) || 0;
    return { top: top, bottom: bottom };
  }

  function focusedBox() {
    var rect = document.activeElement.getBoundingClientRect();
    return { top: rect.top, bottom: rect.bottom, scrollY: window.scrollY };
  }

  function visibleFocus(row) {
    assert(row.contains(document.activeElement), "Focus left the moved item");
    assert(!document.activeElement.disabled, "Focus landed on a disabled control");
    var rect = focusedBox();
    var view = viewport();
    assert(rect.top >= view.top + 5, "Focused control is above the unobscured viewport: " + JSON.stringify(rect));
    assert(rect.bottom <= view.bottom - 5, "Focused control is behind the action bar or below the viewport: " + JSON.stringify(rect));
    return rect;
  }

  function mount(keyboardOnly) {
    if (currentCard) { currentCard.cleanup(); }
    currentCard = window.LearningStudioRenderers.render({
      type: "sequence_order",
      payload: {
        prompt: "Put twenty steps in order",
        content: {
          steps: Array.from({ length: 20 }, function (_, index) {
            return { id: "step-" + index, text: "Step " + (index + 1) };
          }),
        },
      },
    }, {
      t: window.LearningStudioI18n.translator("en"),
      uiLocale: "en",
      keyboardOnly: keyboardOnly,
    });
    main.replaceChildren(currentCard.element);
    window.scrollTo({ top: 0, left: 0, behavior: "instant" });
    assert(document.documentElement.scrollHeight > window.innerHeight,
      "Reduce the browser height so this twenty-item list overflows");
    return main.querySelector(".ordered li");
  }

  function check(name, run) {
    try {
      results.checks.push({ name: name, passed: true, detail: run() });
    } catch (error) {
      results.checks.push({ name: name, passed: false, detail: error.message });
    }
  }

  function display() {
    results.passed = results.checks.every(function (result) { return result.passed; }) &&
      results.pointer.status !== "fail";
    report.textContent = results.checks.map(function (result) {
      return (result.passed ? "PASS " : "FAIL ") + result.name + "\n" + JSON.stringify(result.detail);
    }).join("\n") + "\nNative pointer: " + results.pointer.status;
    report.setAttribute("data-result", results.passed ? "pass" : "fail");
  }

  check("Repeated down moves remain above the sticky footer", function () {
    var row = mount(true);
    row.querySelectorAll("button")[1].focus();
    var final;
    for (var index = 1; index < 20; index += 1) {
      document.activeElement.click();
      assert(currentCard.read().response.order[index] === "step-0", "Down moved the wrong item");
      final = visibleFocus(row);
    }
    return final;
  });

  check("Repeated up moves remain inside the viewport", function () {
    var row = mount(true);
    var buttons = row.querySelectorAll("button");
    for (var index = 1; index < 20; index += 1) { buttons[1].click(); }
    buttons[0].focus();
    var final;
    for (var position = 18; position >= 0; position -= 1) {
      document.activeElement.click();
      assert(currentCard.read().response.order[position] === "step-0", "Up moved the wrong item");
      final = visibleFocus(row);
    }
    return final;
  });

  check("Undo reveals the restored offscreen item", function () {
    var row = mount(true);
    row.querySelectorAll("button")[1].focus();
    document.activeElement.click();
    var undo = main.querySelector(".order-undo");
    undo.focus();
    var before = focusedBox();
    assert(row.getBoundingClientRect().bottom < viewport().top,
      "Undo setup did not move the original item offscreen; reduce browser height");
    undo.click();
    assert(currentCard.read().response.order[0] === "step-0", "Undo did not restore the item");
    return { before: before, after: visibleFocus(row) };
  });

  display();
  mount(false);

  document.getElementById("prepare-pointer").addEventListener("click", function () {
    pointerCheck = null;
    var row = mount(false);
    var focused = row.querySelectorAll("button")[1];
    focused.focus({ preventScroll: true });
    window.scrollBy({ top: row.getBoundingClientRect().bottom + 48, left: 0, behavior: "instant" });
    var view = viewport();
    var visible = Array.from(main.querySelectorAll(".ordered li")).filter(function (entry) {
      var rect = entry.getBoundingClientRect();
      return rect.top > view.top + 48 && rect.bottom < view.bottom - 48;
    });
    assert(visible.length >= 2, "Increase browser height enough to show two full rows");
    assert(row.getBoundingClientRect().bottom < view.top, "Pointer setup must leave the focused row offscreen");
    pointerCheck = {
      focused: focused,
      scrollY: window.scrollY,
      order: JSON.stringify(currentCard.read().response.order),
    };
    results.pointer = { status: "ready", scrollY: window.scrollY };
    instructions.textContent = "Drag the handle of " + visible[0].querySelector(".text").textContent +
      " below the midpoint of " + visible[1].querySelector(".text").textContent +
      ". The page must stay at its current scroll position, with focus on the original offscreen row.";
    display();
  });

  window.addEventListener("pointerup", function () {
    if (!pointerCheck) { return; }
    window.requestAnimationFrame(function () {
      if (!pointerCheck || JSON.stringify(currentCard.read().response.order) === pointerCheck.order) { return; }
      try {
        assert(document.activeElement === pointerCheck.focused, "Pointer reorder did not restore the original focus");
        assert(Math.abs(window.scrollY - pointerCheck.scrollY) < 1, "Pointer focus recovery scrolled the page");
        results.pointer = { status: "pass", scrollY: window.scrollY, focusedBox: focusedBox() };
      } catch (error) {
        results.pointer = { status: "fail", detail: error.message };
      }
      pointerCheck = null;
      display();
    });
  });
})();
