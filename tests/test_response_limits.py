"""One coherent limit contract, from the manifest to the request body.

The defect this pins down: the component registry accepted ``min_words`` up to
5,000 while the API refused any response string over 4,000 characters. The
shortest text containing *n* words needs ``2n − 1`` characters, so a
``min_words: 5000`` component validated, stored, rendered — and then refused
every possible answer a learner could type. An accepted manifest that cannot be
completed is worse than a rejected one, because nobody finds out until a learner
is stuck.

The bound is now derived rather than chosen, and these tests assert the whole
chain agrees: schema, runtime validation, the textarea's own ``maxlength``, the
frontend's word check, the API's per-string ceiling, and the HTTP body ceiling.
"""

from __future__ import annotations

import inspect
import json
import re

import pytest

from learning_studio.components import (
    MAX_WORDS,
    MINIMUM_REQUEST_BYTES,
    PASSAGE_MAX,
    RESPONSE_CHARS_MAX,
    ComponentError,
    build_component,
)
from learning_studio.config import LearningStudioConfig
from learning_studio.web.security import (
    MAX_RESPONSE_CHARS,
    MAX_RESPONSE_ITEMS,
    InvalidResponseValue,
    validate_response_value,
)
from learning_studio.web.static_files import STATIC_DIR


def shortest_text_of(word_count: int) -> str:
    """The shortest string that counts as ``word_count`` words."""
    return " ".join("w" * 1 for _ in range(word_count))


def open_component(component_type: str, content: dict):
    """An open-response component of ``component_type`` with ``content``."""
    rubric = [
        {
            "criterion": "depth",
            "levels": [{"label": "Secure", "descriptor": "Thorough.", "points": 3}],
        }
    ]
    return build_component(
        {
            "id": "limits-01",
            "type": component_type,
            "prompt": "Explain your reasoning.",
            "content": content,
            "evaluation": {"rubric": rubric, "scoring": {"mode": "rubric", "points": 6}},
        },
        "component",
    )


# ── The numbers are one decision ──────────────────────────────────────────


def test_the_component_word_bound_is_derived_from_the_api_character_limit():
    """If these ever drift, an accepted manifest becomes uncompletable again."""
    assert RESPONSE_CHARS_MAX == MAX_RESPONSE_CHARS
    assert MAX_WORDS == (MAX_RESPONSE_CHARS + 1) // 2


def test_the_maximum_word_count_is_actually_writable():
    """The whole point: the largest accepted requirement has a legal answer."""
    text = shortest_text_of(MAX_WORDS)

    assert len(text.split()) == MAX_WORDS
    assert len(text) <= MAX_RESPONSE_CHARS
    assert validate_response_value({"text": text}) == {"text": text}


def test_one_word_more_than_the_maximum_cannot_be_written():
    """Confirms the bound is exactly at the edge and not merely safe."""
    text = shortest_text_of(MAX_WORDS + 1)

    assert len(text) > MAX_RESPONSE_CHARS
    with pytest.raises(InvalidResponseValue):
        validate_response_value({"text": text})


@pytest.mark.parametrize("field", ["min_words", "max_words"])
def test_the_schema_accepts_the_bound_and_refuses_one_above_it(field: str):
    component = open_component("rubric_response", {field: MAX_WORDS})
    assert component.content[field] == MAX_WORDS

    with pytest.raises(ComponentError):
        open_component("rubric_response", {field: MAX_WORDS + 1})


def test_the_json_schema_states_the_same_bound_as_the_runtime():
    from learning_studio.components import SPEC_BY_TYPE, object_schema

    schema = object_schema(SPEC_BY_TYPE["rubric_response"].content)

    assert schema["properties"]["min_words"]["maximum"] == MAX_WORDS
    assert schema["properties"]["max_words"]["maximum"] == MAX_WORDS


@pytest.mark.parametrize(
    "component_type", ["free_response", "rubric_response", "reflection", "self_explanation"]
)
def test_every_word_bounded_type_shares_the_bound(component_type: str):
    from learning_studio.components import SPEC_BY_TYPE

    for field in SPEC_BY_TYPE[component_type].content:
        if field.name in {"min_words", "max_words"}:
            assert field.maximum == MAX_WORDS, f"{component_type}.{field.name}"


# ── Every accepted manifest admits at least one accepted response ─────────


@pytest.mark.parametrize("word_count", [1, 2, 100, MAX_WORDS - 1, MAX_WORDS])
def test_a_minimum_at_any_accepted_value_can_be_satisfied(word_count: int):
    """Walks the range rather than only the corner."""
    open_component("free_response", {"min_words": word_count})
    text = shortest_text_of(word_count)

    assert len(text.split()) >= word_count
    assert len(text) <= MAX_RESPONSE_CHARS
    assert validate_response_value({"text": text})


def test_a_satisfying_response_also_fits_the_http_body_ceiling():
    """The per-string limit is not the only one a request has to clear."""
    limit = LearningStudioConfig().mini_app_max_request_bytes
    body = json.dumps({"component_id": "c" * 64, "response": {"text": shortest_text_of(MAX_WORDS)}})

    assert len(body.encode("utf-8")) < limit


def test_a_multi_prompt_minimum_fits_when_spread_across_every_prompt():
    """`min_words` is a total, so eight prompts do not multiply the requirement.

    The aggregate has to clear the body ceiling too, which is why the total
    interpretation is the safe one: the per-field reading would demand eight
    times the words and eight times the bytes.
    """
    from learning_studio.components import MAX_PROMPT_LIST

    limit = LearningStudioConfig().mini_app_max_request_bytes
    per_prompt = shortest_text_of(max(1, MAX_WORDS // MAX_PROMPT_LIST))
    body = json.dumps(
        {"component_id": "c", "response": {"responses": [per_prompt] * MAX_PROMPT_LIST}}
    )

    assert len(" ".join([per_prompt] * MAX_PROMPT_LIST).split()) >= MAX_WORDS - MAX_PROMPT_LIST
    assert len(body.encode("utf-8")) < limit
    assert validate_response_value({"responses": [per_prompt] * MAX_PROMPT_LIST})


def test_a_seeded_passage_fits_the_response_limit_it_will_be_submitted_under():
    """`error_correction` and `code_response` pre-fill the field they submit."""
    assert PASSAGE_MAX <= MAX_RESPONSE_CHARS


# ── Word counting is the same on both sides ───────────────────────────────

#: The frontend's counter and any server-side reading of a word must agree, or a
#: learner passes the local check and is refused by the API -- or worse, is told
#: to write more when they already have.
#:
#: Shared with the JavaScript suite through the generated fixture file, so both
#: implementations are checked against *the same* cases rather than against two
#: lists that drift. Unicode separators are here on purpose: Python's `\s` and
#: JavaScript's `\s` both treat U+00A0 and U+2003 as separators and both treat
#: U+200B as not one, and "they agree" is the property worth pinning rather than
#: assuming -- an earlier version of this comment asserted a divergence that does
#: not exist.
WORD_CASES: list[tuple[str, int]] = [
    ("one two three", 3),
    ("  leading and trailing  ", 3),
    ("multiple   internal   spaces", 3),
    ("line\nbreaks\tand\ttabs", 4),
    ("hyphenated-words count once", 3),
    ("punctuation, alone; counts.", 3),
    ("no-break\u00a0space\u00a0separated", 3),
    ("em\u2003quad\u2003spaces", 3),
    ("zero\u200bwidth\u200bjoined", 1),
    ("", 0),
    ("   ", 0),
]


@pytest.mark.parametrize(("text", "expected"), WORD_CASES)
def test_word_counting_follows_one_documented_rule(text: str, expected: int):
    """Whitespace-separated tokens, exactly as the frontend's regex does it."""
    stripped = text.strip()
    counted = len(re.split(r"\s+", stripped)) if stripped else 0

    assert counted == expected
    # `str.split()` is the same rule spelled differently, and is what a
    # server-side reader would reach for first.
    assert len(text.split()) == expected


def test_the_frontend_counts_words_by_whitespace_runs():
    source = (STATIC_DIR / "renderers.js").read_text(encoding="utf-8")

    assert "trimmed.split(/\\s+/).length" in source


def test_the_textarea_limit_is_the_api_limit():
    """A field that let a learner type more than the API accepts would produce a
    400 that looked like a server fault."""
    source = (STATIC_DIR / "renderers.js").read_text(encoding="utf-8")

    assert f"var MAX_TEXT = {MAX_RESPONSE_CHARS};" in source


# ── Direct API submissions are bounded too ────────────────────────────────


def test_an_overlong_string_is_refused_whatever_field_it_arrives_in():
    too_long = "x" * (MAX_RESPONSE_CHARS + 1)

    for shape in ({"text": too_long}, {"responses": [too_long]}, {"code": too_long}):
        with pytest.raises(InvalidResponseValue):
            validate_response_value(shape)


def test_an_exact_limit_string_is_accepted():
    exact = "x" * MAX_RESPONSE_CHARS

    assert validate_response_value({"text": exact}) == {"text": exact}


def test_too_many_response_items_are_refused():
    with pytest.raises(InvalidResponseValue):
        validate_response_value({"responses": ["ok"] * (MAX_RESPONSE_ITEMS + 1)})


def test_the_aggregate_ceiling_is_the_request_body_limit_not_the_string_limit():
    """Several maximal strings pass the per-string check and must still be
    stopped by the body limit, which is the only aggregate bound there is."""
    limit = LearningStudioConfig().mini_app_max_request_bytes
    maximal = "x" * MAX_RESPONSE_CHARS
    payload = {"responses": [maximal] * 8}

    assert validate_response_value(payload) == payload
    assert len(json.dumps(payload).encode("utf-8")) > limit


# ── Every accepted configuration, not just the default one ────────────────


def accepted_request_sizes() -> list[int]:
    """The minimum, the default, and the maximum an operator may configure."""
    import re

    import learning_studio.config as configuration

    source = inspect.getsource(configuration)
    match = re.search(
        r'"mini_app_max_request_bytes": lambda raw, key: _bounded_int\(\s*raw, key, '
        r"([A-Z_]+|\d[\d_]*), ([\d_]+)\s*\)",
        source,
    )
    assert match, "the request-size bound is no longer declared where this test reads it"
    low = (
        MINIMUM_REQUEST_BYTES
        if match.group(1) == "MINIMUM_REQUEST_BYTES"
        else int(match.group(1).replace("_", ""))
    )
    high = int(match.group(2).replace("_", ""))
    return [low, LearningStudioConfig().mini_app_max_request_bytes, high]


def worst_case_body() -> bytes:
    """The largest body the *shortest* satisfying answer could ever need.

    The most demanding accepted manifest: the maximum word requirement, spread
    across the maximum number of prompts, with a full-length component id. If this
    fits, everything fits.
    """
    from learning_studio.components import MAX_PROMPT_LIST

    per_prompt = -(-MAX_WORDS // MAX_PROMPT_LIST)
    return json.dumps(
        {
            "component_id": "c" * 64,
            "response": {"responses": [shortest_text_of(per_prompt)] * MAX_PROMPT_LIST},
        }
    ).encode("utf-8")


def test_the_configured_floor_is_derived_from_the_component_contract():
    """Not a chosen number: below it, an accepted manifest is unanswerable."""
    assert accepted_request_sizes()[0] == MINIMUM_REQUEST_BYTES
    assert MINIMUM_REQUEST_BYTES > RESPONSE_CHARS_MAX


@pytest.mark.parametrize("configured", accepted_request_sizes())
def test_the_worst_accepted_manifest_fits_every_accepted_configuration(configured: int):
    """The reported defect: 512 bytes was accepted, and 2,000 words did not fit."""
    body = worst_case_body()

    assert len(body) <= configured, (
        f"a manifest the registry accepts needs {len(body)} bytes, "
        f"which a configuration of {configured} refuses"
    )


@pytest.mark.parametrize("configured", accepted_request_sizes())
def test_a_single_field_maximum_also_fits_every_accepted_configuration(configured: int):
    body = json.dumps(
        {"component_id": "c" * 64, "response": {"text": shortest_text_of(MAX_WORDS)}}
    ).encode("utf-8")

    assert len(body) <= configured


def test_a_configuration_below_the_floor_is_refused():
    from learning_studio.config import CONFIG_SECTION, ConfigError

    for refused in (1, 512, MINIMUM_REQUEST_BYTES - 1):
        with pytest.raises(ConfigError):
            LearningStudioConfig.from_mapping(
                {CONFIG_SECTION: {"mini_app_max_request_bytes": refused}}
            )


def test_the_floor_itself_is_accepted():
    from learning_studio.config import CONFIG_SECTION

    parsed = LearningStudioConfig.from_mapping(
        {CONFIG_SECTION: {"mini_app_max_request_bytes": MINIMUM_REQUEST_BYTES}}
    )

    assert parsed.mini_app_max_request_bytes == MINIMUM_REQUEST_BYTES


def test_the_floor_leaves_room_for_the_envelope_not_just_the_answer():
    """A ceiling equal to the character limit would forget the JSON around it."""
    body = worst_case_body()

    assert len(body) > RESPONSE_CHARS_MAX, "the envelope should cost something"
    assert len(body) <= MINIMUM_REQUEST_BYTES
