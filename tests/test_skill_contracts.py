"""Textual contract tests over the agent-facing guidance.

The skill has no runtime, so its guarantees live in prose. These tests pin the
statements a reader of the skill must not be able to lose in a rewrite: what may
launch without asking, what happens when tools are absent, that exercises are
data rather than generated code, how images enter the system, and who owns
memory. Matching is done on whitespace-normalised lowercase text so that
re-wrapping a paragraph never breaks a test.
"""

from __future__ import annotations

import re

import pytest


def normalize(text: str) -> str:
    return " ".join(text.lower().split())


def assert_states(text: str, patterns: tuple[str, ...], contract: str) -> None:
    """Every pattern must appear — each one pins a separate clause."""
    missing = [p for p in patterns if not re.search(p, normalize(text))]
    assert missing == [], f"guidance does not state the {contract} contract; unmatched: {missing}"


# ── Activation policy ──────────────────────────────────────────────────────


def test_explicit_learner_request_may_launch_without_extra_confirmation(corpus: str):
    assert_states(
        corpus,
        (
            r"explicit(ly)? (learner |user )?request",
            r"(launch|start|open)[^.]{0,80}without (asking|further|extra|additional)",
        ),
        "explicit-request activation",
    )


def test_agent_initiated_practice_requires_confirmation(corpus: str):
    assert_states(
        corpus,
        (
            r"(you|the agent) (suggest|propose|recommend|initiate)",
            r"(confirm|confirmation|agree|says yes)",
        ),
        "agent-initiated confirmation",
    )


def test_activation_policy_is_summarised_in_the_skill_itself(skill_md: str):
    """The rule must survive even if the agent never opens the reference."""
    assert_states(
        skill_md,
        (r"activation", r"confirm"),
        "activation summary in SKILL.md",
    )


# ── Fallback to chat ───────────────────────────────────────────────────────


def test_missing_tools_fall_back_to_chat(corpus: str):
    assert_states(
        corpus,
        (
            r"(not (yet )?(available|registered|installed)|are (absent|missing)|no tools)",
            r"(continue|carry on|run|keep going)[^.]{0,80}(in|as) (chat|conversation)",
        ),
        "chat fallback",
    )


def test_never_claims_an_exercise_was_started(corpus: str):
    assert_states(
        corpus,
        (
            r"never (say|claim|imply|tell|pretend|report)",
            r"(mini app|exercise|session)[^.]{0,80}(started|launched|opened|running)",
        ),
        "no-false-launch",
    )


def test_skill_does_not_advertise_tools_that_do_not_exist(corpus: str, ctx):
    """Extends the PR-01 guard to every reference file.

    ``register()`` still registers no tools, so any tool-call syntax anywhere in
    the skill corpus would send the agent after something that cannot answer.
    """
    from learning_studio import register

    register(ctx)
    assert ctx.tools == [], "this guard assumes no tools are registered yet"

    for token in ("learning_studio_", "plugin_learning_studio("):
        assert token not in corpus, f"guidance references '{token}' but no such tool is registered"


# ── Exercises are declarative, never generated code ────────────────────────


def test_exercises_are_data_not_generated_code(corpus: str):
    assert_states(
        corpus,
        (
            r"(declarative|data|manifest)",
            r"never (write|generate|emit|produce|author)[^.]{0,90}"
            r"(code|html|javascript|css|markup|script)",
        ),
        "generated-code prohibition",
    )


def test_generated_code_prohibition_appears_in_the_manifest_contract(references: dict[str, str]):
    assert_states(
        references.get("manifest-contract", ""),
        (r"never (write|generate|emit|produce|author)", r"(html|javascript|code)"),
        "manifest-contract code prohibition",
    )


# ── Image workflow ─────────────────────────────────────────────────────────


def test_images_come_from_the_host_image_tool(corpus: str):
    assert_states(
        corpus,
        (r"image_generate|host agent's (existing )?image", r"import"),
        "image generation",
    )


def test_asset_identifiers_are_never_invented(corpus: str):
    assert_states(
        corpus,
        (
            r"never (invent|fabricate|make up|guess)[^.]{0,80}(asset|id|identifier|path|filename)",
            r"(real|actual) tool result",
        ),
        "asset-identifier honesty",
    )


# ── Memory ownership ───────────────────────────────────────────────────────


def test_detailed_progress_belongs_to_studio_storage(corpus: str):
    assert_states(
        corpus,
        (
            r"(detailed )?progress[^.]{0,80}(studio|sqlite)",
            r"(attempt|score)s?[^.]{0,120}(never|not|do not|don't)[^.]{0,60}"
            r"(hermes memory|global memory)"
            r"|(never|do not|don't)[^.]{0,120}(attempt|score)s?[^.]{0,80}"
            r"(hermes memory|global memory)",
        ),
        "progress storage ownership",
    )


def test_only_the_agent_writes_hermes_memory(corpus: str):
    assert_states(
        corpus,
        (r"only the agent[^.]{0,60}memory", r"replace[^.]{0,90}(stale|outdated|superseded)"),
        "memory write ownership",
    )


def test_multi_user_memory_warning_is_present(corpus: str):
    assert_states(
        corpus,
        (
            r"(dedicated to (a )?(one|single) learner|single-learner profile)",
            r"(isolated per user|per-user isolation|verified[^.]{0,60}isolat)",
            r"per-user studio storage|studio storage[^.]{0,60}per user",
        ),
        "multi-user memory warning",
    )


def test_multi_user_warning_is_visible_in_the_skill_itself(skill_md: str):
    """A privacy rule buried in a reference file may never be read."""
    assert_states(skill_md, (r"memory", r"learner"), "memory warning in SKILL.md")


# ── Subject neutrality ─────────────────────────────────────────────────────

#: Concrete, unambiguous domain markers. Deliberately excludes words that the
#: guidance also uses in a subject-neutral, pedagogical sense — "language"
#: (the discovery dimensions), "vocabulary" (a property of any deck) — since
#: counting those would credit a domain for text that illustrates nothing.
DOMAIN_MARKERS = {
    "language learning": ("spanish", "japanese", "kanji", "conjugation", "preterite"),
    "programming": ("python", "sql", "recursion", "regex", "compiler", "pointer"),
    "history": ("renaissance", "cold war", "treaty", "meiji", "ottoman", "suffrage"),
    "science": ("mitosis", "photosynthesis", "titration", "enzyme", "newton", "orbital"),
}


def domain_counts(text: str) -> dict[str, int]:
    normalized = normalize(text)
    return {
        domain: sum(normalized.count(marker) for marker in markers)
        for domain, markers in DOMAIN_MARKERS.items()
    }


@pytest.mark.parametrize("domain", sorted(DOMAIN_MARKERS))
def test_skill_illustrates_every_domain(skill_md: str, domain: str):
    assert domain_counts(skill_md)[domain] > 0, (
        f"SKILL.md never illustrates {domain}; the workflow must read as subject-agnostic"
    )


def test_no_domain_dominates_the_corpus(corpus: str):
    """No subject may read as the plugin's default."""
    counts = domain_counts(corpus)
    total = sum(counts.values())
    assert total > 0
    heaviest, hits = max(counts.items(), key=lambda item: item[1])
    assert hits / total <= 0.4, (
        f"{heaviest} accounts for {hits}/{total} of subject examples — "
        f"no subject may be presented as the default (counts: {counts})"
    )


def test_frontmatter_description_names_no_subject(skill_md: str):
    """The description is the only text every agent sees; it must stay neutral."""
    frontmatter = skill_md.partition("---\n")[2].partition("\n---\n")[0]
    counts = domain_counts(frontmatter)
    assert sum(counts.values()) == 0, f"frontmatter favours a subject: {counts}"


@pytest.mark.parametrize(
    "name",
    [
        "selection-cards",
        "text-input-cards",
        "ordering-and-matching",
        "flashcards-and-recall",
        "media-cards",
        "diagrams-and-hotspots",
        "timelines-and-processes",
        "tables-and-grids",
        "scenarios-and-simulations",
        "reflection-and-rubrics",
    ],
)
def test_ui_reference_examples_span_unrelated_subjects(references: dict[str, str], name: str):
    counts = domain_counts(references.get(name, ""))
    represented = sorted(domain for domain, hits in counts.items() if hits)
    assert len(represented) >= 3, (
        f"references/{name}.md illustrates only {represented}; "
        f"examples must span unrelated subjects"
    )
