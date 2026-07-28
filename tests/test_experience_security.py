"""What a prepared exercise may contain, and what this feature may do.

Two halves:

- **Content.** Everything a manifest carries is inert text. Markup, scripts,
  stylesheets, event handlers, URLs, filesystem paths and credential-shaped
  values are refused, in learner-visible and evaluator-only fields alike.
- **Scope.** Preparing an exercise executes nothing, fetches nothing, renders
  nothing, and reaches no optional dependency. Proved by parsing the source
  rather than by grepping it, so a comment promising not to use ``subprocess``
  does not read as a violation and an import hidden in a function does.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from learning_studio.manifest import ManifestError, build_manifest
from learning_studio.safety import UnsafeContent, safe_text
from tests.component_examples import example, manifest

PACKAGE = Path(__file__).resolve().parent.parent / "learning_studio"

#: The modules this PR introduced. Scanned specifically as well as with the
#: whole package, so a scan that silently stopped finding files still fails.
NEW_MODULES = ("safety.py", "components.py", "manifest.py")


def sources() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def new_sources() -> list[Path]:
    return [PACKAGE / name for name in NEW_MODULES]


def test_the_new_modules_exist_to_be_scanned():
    """Guards every scan below against passing because it found nothing."""
    for path in new_sources():
        assert path.is_file(), f"{path.name} is missing"


# ── Content safety: what a manifest may carry ─────────────────────────────


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("script tag", "Read this <script>alert(1)</script> carefully"),
        ("closing tag", "The answer is </p> obviously"),
        ("html comment", "Consider <!-- hidden --> the following"),
        ("img tag", "What does <img src=x onerror=alert(1)> show?"),
        ("event handler", 'Click here onclick="steal()" to continue'),
        ("style tag", "Look at <style>body{color:red;}</style> this"),
        ("stylesheet rule", "Apply {color: red; font-size: 2em;} to the box"),
        ("css import", "The rule @import url(evil) is what you need"),
        ("javascript url", "Follow javascript:alert(document.cookie) now"),
        ("data url", "Decode data:text/html;base64,PHNjcmlwdD4="),
        ("http url", "Read more at https://example.invalid/page"),
        ("bare scheme", "Fetch it from ftp://files.invalid/x"),
        ("html entity", "Escape it as &lt;script&gt; in your answer"),
        ("traversal", "Open ../../etc/passwd and describe it"),
        ("absolute path", "The log lives at /var/log/system/messages"),
        ("windows path", "Open C:\\Users\\someone\\notes.txt"),
        ("home path", "It is in ~/private/notes/ somewhere"),
        ("api key", "Use api_key=sk-live-ABCDEFGHIJKLMNOPQRSTUV to connect"),
        ("password", "The password: hunter2correcthorse"),
        ("bearer token", "Send Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345"),
        ("private key", "-----BEGIN RSA PRIVATE KEY-----"),
        ("aws key", "The key AKIAIOSFODNN7EXAMPLE is used here"),
        ("jwt", "Present eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc"),
        # Bypasses found in review. Each was accepted by the first version.
        ("css without a semicolon", "Apply body { color: red } to the page"),
        ("css shorthand block", "The rule is {margin: 0 auto}"),
        ("bare hostname", "Visit www.example.invalid/path for more"),
        ("bare domain and path", "See example.com/reference for the table"),
        ("mailto uri", "Email mailto:someone@example.invalid with questions"),
        ("ftp uri", "Fetch ftp:files.example.invalid"),
        ("file uri", "Open file:notes.txt to continue"),
        ("relative dot path", "Open ./secret.txt and read the first line"),
        ("single-segment absolute path", "Open /secret.txt for the answer"),
        ("home file path", "It is in ~/notes.txt somewhere"),
        # A slash-prefixed command is lexically a path, and "/secret" and
        # "/help" cannot be told apart. The contract says no paths, so both
        # are refused and a command is written without its slash.
        ("slash command", "Press /help to list the commands"),
        ("unc path", "Copy it from \\\\server\\share"),
        ("basic authorization", "Send Authorization: Basic dXNlcjpwYXNzd29yZA=="),
        ("bare basic credential", "Use Basic YWxhZGRpbjpvcGVuc2VzYW1l as the header"),
        ("project-scoped token", "Use sk-proj-ABCDEFGHIJKLMNOPQRSTUV to connect"),
        ("github fine-grained token", "Use github_pat_ABCDEFGHIJKLMNOPQRSTUVWX here"),
        ("gitlab token", "Use glpat-ABCDEFGHIJKLMNOPQRST for CI"),
        ("google api key", "Use AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ012345 for maps"),
        ("slack app token", "Use xapp-1-ABCDEFGHIJKLMNOP for the socket"),
        ("huggingface token", "Use hf_ABCDEFGHIJKLMNOPQRSTUVWXYZ01 to download"),
        # Bypasses found in the final adversarial review.
        ("long top-level domain", "Visit example.museum for the archive"),
        ("uncommon top-level domain", "Visit malware.tech today"),
        ("one-letter scheme", "Use x:payload now"),
        ("two-letter scheme", "Open ms:settings here"),
        ("path after a bracket", "Open (/etc/passwd) now"),
        ("path after a straight quote", 'Open "/etc/hosts" now'),
        ("path after a typographic quote", "Open “/etc/shadow” now"),
        ("bracketed ipv6 with a port", "Connect to [::1]:8080/admin"),
        ("bracketed ipv6 alone", "Connect to [2001:db8::1] first"),
        ("one-letter scheme with digit payload", "Use x:1 now"),
        ("hyphenated domain with two-letter tld", "Visit example-site.io"),
        ("digit-bearing domain with two-letter tld", "Visit example1.io"),
        ("short digit-bearing host", "Visit foo2.uk"),
        ("one-character domain label", "Visit x.museum"),
        ("path after CJK corner brackets", "Open 「/etc/passwd」"),
        ("path after CJK lenticular brackets", "Open 【/etc/passwd】"),
        ("unicode absolute path", "Open /école/data"),
        ("unicode punctuation-wrapped path", "Open 《/etc/passwd》"),
        ("compatibility-form uri", "Use ｘ：１ now"),
        ("compatibility-form path", "Open ／etc／passwd"),
    ],
)
def test_unsafe_content_is_refused_in_a_prompt(label: str, text: str):
    component = example("true_false", prompt=text)

    with pytest.raises(ManifestError):
        build_manifest(manifest([component]))


@pytest.mark.parametrize(
    "text",
    [
        "If a < b and b < c, then a < c",
        "Solve for x where 3 < x < 9",
        "The reaction is exothermic (ΔH < 0)",
        "Compare and/or contrast the two accounts",
        "The ratio was 3/4 by the end of 1918",
        "Give the date as 12/05/2026",
        "Explain what makes a password strong",
        "Discuss why the data: numbers alone rarely settle an argument",
        # Near misses for the rules added after review. Each of these is
        # ordinary educational prose and must keep working.
        "Set A = {x : x > 0} contains the positive reals",
        "Node.js and Deno both run JavaScript outside a browser",
        "The .org suffix was originally for organisations",
        "Write the file: notes.md, then continue",
        "Note: see the appendix for the full derivation",
        "In 2019 the ratio was 2/3, and by 2021 it was 3/4",
        "The basic idea is that pressure and volume vary inversely",
        "A secret ballot is one where nobody can see how you voted",
        "Compare the mean and the median of the data set",
        # Near misses for the rules tightened in the final review. Ordinary
        # punctuation must not turn prose into a locator.
        "He said “the answer is four” loudly",
        "Use the (correct) form of the verb",
        "The interval [1:2] is half-open",
        "Node.js and index.md are both file names",
        "U.S.A. was formed in 1776",
        "e.g. i.e. and etc. are abbreviations",
        "Chapter 3: the aftermath",
        "The ratio 3:4 applies here",
        "Meet at 12:30 sharp",
        "Solve for x: the value is 7",
    ],
)
def test_ordinary_content_is_not_mistaken_for_an_attack(text: str):
    """The rules must not reject the mathematics and prose people actually write."""
    assert safe_text(text, "prompt", max_chars=500) == text


def test_unsafe_content_is_refused_in_an_evaluator_only_field():
    """Hidden today is not hidden forever; the rules apply to both halves."""
    component = example("multiple_choice")
    component["evaluation"]["notes"] = "See <script>steal()</script> for context"

    with pytest.raises(ManifestError, match="markup"):
        build_manifest(manifest([component]))


def test_unsafe_content_is_refused_inside_an_answer_key():
    component = example("short_answer")
    component["answer"]["accepted"] = ["<b>photosynthesis</b>"]

    with pytest.raises(ManifestError, match="markup"):
        build_manifest(manifest([component]))


def test_an_asset_reference_cannot_be_a_url_or_a_path():
    for bad in ("https://example.invalid/x.png", "../../secret.png", "/etc/passwd"):
        component = example("hotspot")
        component["content"]["image"]["asset_ref"] = bad
        with pytest.raises(ManifestError):
            build_manifest(manifest([component]))


def test_invisible_and_bidirectional_characters_are_refused():
    """Text that renders differently from how it reads cannot be reviewed."""
    with pytest.raises(UnsafeContent, match="invisible"):
        safe_text("Choose the \u202ecorrect\u202c answer", "prompt", max_chars=500)


def test_control_characters_are_refused():
    with pytest.raises(UnsafeContent, match="control"):
        safe_text("Choose\x00the answer", "prompt", max_chars=500)


def test_code_as_subject_matter_is_accepted_when_it_carries_no_markup():
    """The restriction is on markup, not on programming as a subject."""
    component = example(
        "code_response",
        content={
            "language": "python",
            "starter_code": "def median(values):\n    if not values:\n        return None",
        },
    )

    built = build_manifest(manifest([component]))

    assert built.components[0].content["language"] == "python"


# ── Scope: this feature runs nothing and fetches nothing ──────────────────

#: Names that execute code. ``compile`` is included because it is the step
#: before ``exec``, and a plugin that never runs code has no reason to reach
#: for any of them.
EXECUTION_CALLS = (
    "eval",
    "exec",
    "compile",
    "__import__",
    "os.system",
    "os.popen",
    "os.execv",
    "os.spawnv",
    "os.fork",
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_output",
)

EXECUTION_MODULES = ("subprocess", "multiprocessing", "pty", "ctypes", "runpy")

#: Anything that could reach the network. The plugin's dependency list is
#: empty and must stay that way; a network call in a manifest validator is a
#: server-side request forgery waiting for a URL field to exist.
NETWORK_MODULES = ("httpx", "requests", "urllib", "http", "socket", "aiohttp", "ftplib")

#: Optional dependencies that later PRs may introduce. None may be reachable
#: during import or registration, because Hermes registers every enabled
#: plugin at startup on installs that never opted into an extra.
OPTIONAL_MODULES = ("fastapi", "starlette", "uvicorn", "PIL", "telegram", "telethon", "cloudflare")


def imported_names(path: Path) -> set[str]:
    """Top-level packages this file imports. Relative imports are this one."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names.add("learning_studio")
            elif node.module:
                names.add(node.module.split(".")[0])
    return names


def called_names(path: Path) -> set[str]:
    """Bare calls by name, plus dotted calls as ``module.attribute``.

    The distinction matters: ``compile(...)`` turns a string into code and
    ``re.compile(...)`` builds a regex. Collapsing both to "compile" would
    make every module in this package look like it executes code.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            names.add(f"{node.func.value.id}.{node.func.attr}")
    return names


@pytest.mark.parametrize("module", NEW_MODULES)
def test_no_new_module_executes_code(module: str):
    """Parsed, not grepped: a docstring saying "never exec" is not a call."""
    path = PACKAGE / module

    offenders = sorted(called_names(path) & set(EXECUTION_CALLS))
    assert offenders == [], f"{module} calls {offenders}"


def test_no_source_in_the_package_executes_code():
    offenders = [
        f"{path.name} calls {sorted(called_names(path) & set(EXECUTION_CALLS))}"
        for path in sources()
        if called_names(path) & set(EXECUTION_CALLS)
    ]
    assert offenders == []


@pytest.mark.parametrize("forbidden", [*EXECUTION_MODULES, *NETWORK_MODULES, *OPTIONAL_MODULES])
def test_the_package_imports_no_execution_network_or_optional_module(forbidden: str):
    offenders = [path.name for path in sources() if forbidden in imported_names(path)]

    assert offenders == [], f"{forbidden} is imported by {offenders}"


def test_the_call_scan_would_catch_a_real_offender(tmp_path: Path):
    """Proves the scans above discriminate rather than passing vacuously."""
    offender = tmp_path / "offender.py"
    offender.write_text("import re\nre.compile('x')\nexec('1')\n", encoding="utf-8")

    found = called_names(offender)

    assert "exec" in found, "a real exec() call was not detected"
    assert "compile" not in found, "re.compile was mistaken for the builtin"


def test_the_new_modules_import_only_the_standard_library_and_this_package():
    """A dependency here would be installed into every Hermes environment."""
    allowed = {
        "__future__",
        "collections",
        "contextlib",
        "dataclasses",
        "typing",
        "json",
        "re",
        "unicodedata",
        "learning_studio",
        "",
    }
    for path in new_sources():
        names = {name for name in imported_names(path) if name}
        unexpected = sorted(names - allowed)
        assert unexpected == [], f"{path.name} imports {unexpected}"


def test_preparing_an_exercise_generates_no_markup():
    """No stored string may contain markup, because none may be authored."""
    built = build_manifest(
        manifest([example(name) for name in ("multiple_choice", "hotspot", "case_study")])
    )

    stored = json.dumps(
        [c.learner_payload() | c.hidden() for c in built.components], ensure_ascii=False
    )
    for markup in ("<div", "<span", "<script", "innerHTML", "document.", "<style"):
        assert markup not in stored


def test_the_component_registry_declares_no_html_or_style_field():
    """A field named for presentation would invite a renderer to trust it."""
    from learning_studio.components import SPECS, component_members

    for spec in SPECS:
        for member in component_members(spec):
            assert member.name not in {"html", "style", "css", "script", "template", "render"}


def test_the_prepare_schema_exposes_no_url_or_path_parameter():
    from learning_studio.schemas import PREPARE_SCHEMA

    offenders: list[str] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for name in node.get("properties", {}):
                words = set(name.lower().split("_"))
                if words & {"url", "uri", "href", "src", "path", "file", "endpoint"}:
                    offenders.append(f"{path}.{name}")
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(PREPARE_SCHEMA["parameters"], "prepare")
    assert offenders == []


def test_preparing_does_not_touch_hermes_memory(hermes_home, gateway_session):
    """The same blocker the context tools are held to, on the new path."""
    import importlib
    import sys

    class Blocker:
        def __init__(self) -> None:
            self.attempts: list[str] = []

        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] in {"memory", "memory_tool", "hermes_memory"}:
                self.attempts.append(fullname)
                raise ImportError(f"{fullname} is blocked")
            return None

    blocker = Blocker()
    saved = {name: mod for name, mod in sys.modules.items() if name.startswith("learning_studio")}
    for name in saved:
        del sys.modules[name]
    sys.meta_path.insert(0, blocker)
    try:
        studio_tools = importlib.import_module("learning_studio.tools")
        result = json.loads(studio_tools.handle_prepare({"manifest": manifest()}))
    finally:
        sys.meta_path.remove(blocker)
        for name in [n for n in sys.modules if n.startswith("learning_studio")]:
            del sys.modules[name]
        sys.modules.update(saved)

    assert result["ok"] is True
    assert blocker.attempts == []


def test_preparing_writes_only_inside_the_studio_directory(hermes_home, gateway_session):
    from learning_studio import tools as studio_tools

    studio_tools.handle_prepare({"manifest": manifest()})

    written = {p for p in hermes_home.rglob("*") if p.is_file()}
    studio = hermes_home / "workspace" / "learning-studio"
    outside = sorted(str(p.relative_to(hermes_home)) for p in written if studio not in p.parents)
    assert outside == []
