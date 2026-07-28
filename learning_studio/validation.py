"""Runtime enforcement of the advertised tool schemas.

A JSON schema is documentation and provider guidance. It is not a guarantee:
not every provider validates before dispatch, tool arguments can arrive from
a replay or a relay, and ``additionalProperties: false`` buys nothing if the
handler then reads whatever it likes. Everything the schema promises is
therefore re-checked here, against the same declarations the schema is built
from.

The two are **not** identical, and the difference is deliberate: the runtime
is stricter. Cross-field rules — an answer that names an option the content
declares, an objective that matches a stored one — and the content-safety
rules that need alternation the two regex dialects disagree about cannot be
put in a JSON Schema, so they live only here and the schema describes them.
``tests/test_schema_parity.py`` pins that relationship in both directions:
nothing the schema accepts may be silently refused later, and where the
runtime is stricter a test names the case.

The checker is a small subset of JSON Schema — objects, arrays, strings,
numbers, integers, booleans, enums, constants, patterns, bounds, uniqueness,
local
``$ref``, and ``oneOf`` — because that is all the tool surface uses, and a
full implementation would be a dependency this plugin does not want. The
advertised schema is checked against a real JSON Schema implementation in the
test suite, where a development dependency costs nobody anything.

Two of those need a word. **Local references** exist because the component
registry states the shapes every type shares once under ``$defs`` instead of
31 times; resolving them here is what keeps the runtime checking the same
declarations the model was shown. **Tagged unions** are dispatched on their
discriminator rather than tried branch by branch: with 31 branches, "does not
match any accepted form" is not an error message, it is a shrug.
"""

from __future__ import annotations

import re
from typing import Any


class SchemaViolation(ValueError):
    """Arguments did not satisfy the advertised schema."""


def _fail(path: str, message: str) -> None:
    where = path or "arguments"
    raise SchemaViolation(f"{where}: {message}")


def validate(
    value: Any, schema: dict[str, Any], path: str = "", root: dict[str, Any] | None = None
) -> None:
    """Validate *value* against *schema*, raising on the first violation.

    Fails fast and fails whole: one bad field rejects the entire request
    rather than letting the good parts through, so a caller never has to
    reason about a half-applied save.

    *root* is the document ``$ref`` resolves against, and defaults to *schema*
    — the tool's own parameters object, which is what a provider treats as the
    root too.
    """
    root = schema if root is None else root

    if "$ref" in schema:
        validate(value, _resolve(schema["$ref"], root, path), path, root)
        return

    if "oneOf" in schema:
        _validate_union(value, schema["oneOf"], path, root)
        return

    expected = schema.get("type")
    if expected == "object":
        _validate_object(value, schema, path, root)
    elif expected == "array":
        _validate_array(value, schema, path, root)
    elif expected == "string":
        _validate_string(value, schema, path)
    elif expected == "integer":
        _validate_integer(value, schema, path)
    elif expected == "number":
        _validate_number(value, schema, path)
    elif expected == "boolean" and not isinstance(value, bool):
        _fail(path, "must be true or false")


def _resolve(pointer: str, root: dict[str, Any], path: str) -> dict[str, Any]:
    """Resolve a local ``#/a/b`` JSON pointer. Only local refs exist here."""
    if not pointer.startswith("#/"):
        _fail(path, "schema uses an unsupported reference")
    node: Any = root
    for segment in pointer[2:].split("/"):
        node = node.get(segment) if isinstance(node, dict) else None
        if node is None:
            _fail(path, "schema reference does not resolve")
    return node


def _validate_union(
    value: Any, options: list[dict[str, Any]], path: str, root: dict[str, Any]
) -> None:
    """Validate against a ``oneOf``, using the discriminator when there is one.

    A tagged union — every branch pinning ``type`` to one constant, which is
    how the component registry is expressed — is dispatched on that tag. The
    alternative, trying every branch and reporting "does not match any
    accepted form", turns a one-character typo inside a 30-branch union into
    an error that says nothing about what was actually wrong.
    """
    branches = _tagged_branches(options)
    if branches is not None:
        declared = value.get("type") if isinstance(value, dict) else None
        if not isinstance(declared, str):
            _fail(path, "must be an object with a 'type' naming its kind")
        if declared not in branches:
            _fail(
                f"{path}.type" if path else "type",
                f"must be one of: {', '.join(sorted(branches))}",
            )
        validate(value, branches[declared], path, root)
        return

    for option in options:
        try:
            validate(value, option, path, root)
        except SchemaViolation:
            continue
        else:
            return
    _fail(path, "does not match any accepted form")


def _tagged_branches(options: list[dict[str, Any]]) -> dict[str, dict[str, Any]] | None:
    """Map tag value to branch, or ``None`` when this is not a tagged union."""
    branches: dict[str, dict[str, Any]] = {}
    for option in options:
        tag = (option.get("properties") or {}).get("type") or {}
        constant = tag.get("const")
        if constant is None:
            allowed = tag.get("enum") or []
            constant = allowed[0] if len(allowed) == 1 else None
        if not isinstance(constant, str):
            return None
        branches[constant] = option
    return branches or None


def _validate_object(value: Any, schema: dict[str, Any], path: str, root: dict[str, Any]) -> None:
    if not isinstance(value, dict):
        _fail(path, "must be an object")

    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(value) - set(properties))
        if unknown:
            _fail(path, f"unknown field(s): {', '.join(unknown)}")

    for required in schema.get("required", []):
        if required not in value:
            _fail(path, f"missing required field '{required}'")

    for key, item in value.items():
        subschema = properties.get(key)
        if subschema is not None:
            validate(item, subschema, f"{path}.{key}" if path else key, root)


def _validate_array(value: Any, schema: dict[str, Any], path: str, root: dict[str, Any]) -> None:
    if not isinstance(value, list):
        _fail(path, "must be an array")

    min_items = schema.get("minItems")
    if min_items is not None and len(value) < min_items:
        _fail(path, f"must have at least {min_items} item(s)")

    max_items = schema.get("maxItems")
    if max_items is not None and len(value) > max_items:
        _fail(path, f"must have at most {max_items} items, got {len(value)}")

    if schema.get("uniqueItems") is True:
        seen: list[Any] = []
        for item in value:
            if item in seen:
                _fail(path, "must not repeat a value")
            seen.append(item)

    item_schema = schema.get("items")
    if item_schema:
        for index, item in enumerate(value):
            validate(item, item_schema, f"{path}[{index}]", root)


def _validate_string(value: Any, schema: dict[str, Any], path: str) -> None:
    if not isinstance(value, str):
        _fail(path, "must be a string")

    constant = schema.get("const")
    if constant is not None and value != constant:
        _fail(path, f"must be '{constant}'")

    enum = schema.get("enum")
    if enum is not None and value not in enum:
        _fail(path, f"must be one of: {', '.join(map(str, enum))}")

    pattern = schema.get("pattern")
    if pattern is not None and not re.search(pattern, value):
        _fail(path, f"must match {pattern}")

    minimum = schema.get("minLength")
    if minimum is not None and len(value.strip()) < minimum:
        _fail(path, f"must be at least {minimum} character(s)")

    maximum = schema.get("maxLength")
    if maximum is not None and len(value) > maximum:
        _fail(path, f"must be at most {maximum} characters, got {len(value)}")


def _validate_integer(value: Any, schema: dict[str, Any], path: str) -> None:
    # bool is an int subclass in Python; `evidence_count: true` is a mistake,
    # not the number 1, and silently reading it as 1 would let a nonsense
    # argument satisfy an evidence threshold.
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(path, "must be an integer")

    minimum = schema.get("minimum")
    if minimum is not None and value < minimum:
        _fail(path, f"must be at least {minimum}")

    maximum = schema.get("maximum")
    if maximum is not None and value > maximum:
        _fail(path, f"must be at most {maximum}")


def _validate_number(value: Any, schema: dict[str, Any], path: str) -> None:
    """``number`` accepts an integer too; ``true`` is still not the number 1."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        _fail(path, "must be a number")

    minimum = schema.get("minimum")
    if minimum is not None and value < minimum:
        _fail(path, f"must be at least {minimum}")

    maximum = schema.get("maximum")
    if maximum is not None and value > maximum:
        _fail(path, f"must be at most {maximum}")
