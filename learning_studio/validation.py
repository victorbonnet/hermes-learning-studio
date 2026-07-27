"""Runtime enforcement of the advertised tool schemas.

A JSON schema is documentation and provider guidance. It is not a guarantee:
not every provider validates before dispatch, tool arguments can arrive from
a replay or a relay, and ``additionalProperties: false`` buys nothing if the
handler then reads whatever it likes. Everything the schema promises is
therefore re-checked here, against the same declarations the schema is built
from, so the two cannot drift.

The checker is a small subset of JSON Schema — objects, arrays, strings,
integers, booleans, enums, and bounds — because that is all the tool surface
uses, and a full implementation would be a dependency this plugin does not
want.
"""

from __future__ import annotations

from typing import Any


class SchemaViolation(ValueError):
    """Arguments did not satisfy the advertised schema."""


def _fail(path: str, message: str) -> None:
    where = path or "arguments"
    raise SchemaViolation(f"{where}: {message}")


def validate(value: Any, schema: dict[str, Any], path: str = "") -> None:
    """Validate *value* against *schema*, raising on the first violation.

    Fails fast and fails whole: one bad field rejects the entire request
    rather than letting the good parts through, so a caller never has to
    reason about a half-applied save.
    """
    if "oneOf" in schema:
        for option in schema["oneOf"]:
            try:
                validate(value, option, path)
            except SchemaViolation:
                continue
            else:
                return
        _fail(path, "does not match any accepted form")

    expected = schema.get("type")
    if expected == "object":
        _validate_object(value, schema, path)
    elif expected == "array":
        _validate_array(value, schema, path)
    elif expected == "string":
        _validate_string(value, schema, path)
    elif expected == "integer":
        _validate_integer(value, schema, path)
    elif expected == "boolean" and not isinstance(value, bool):
        _fail(path, "must be true or false")


def _validate_object(value: Any, schema: dict[str, Any], path: str) -> None:
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
            validate(item, subschema, f"{path}.{key}" if path else key)


def _validate_array(value: Any, schema: dict[str, Any], path: str) -> None:
    if not isinstance(value, list):
        _fail(path, "must be an array")

    max_items = schema.get("maxItems")
    if max_items is not None and len(value) > max_items:
        _fail(path, f"must have at most {max_items} items, got {len(value)}")

    item_schema = schema.get("items")
    if item_schema:
        for index, item in enumerate(value):
            validate(item, item_schema, f"{path}[{index}]")


def _validate_string(value: Any, schema: dict[str, Any], path: str) -> None:
    if not isinstance(value, str):
        _fail(path, "must be a string")

    enum = schema.get("enum")
    if enum is not None and value not in enum:
        _fail(path, f"must be one of: {', '.join(map(str, enum))}")

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
