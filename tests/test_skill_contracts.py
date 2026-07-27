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


#: Host tools verified to exist in Hermes' own toolset registry. The guidance
#: may instruct the agent to call these and nothing else, because this plugin
#: registers no tools of its own.
KNOWN_HOST_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "search_files",
        "patch",
        "skill_view",
        "skills_list",
        "image_generate",
        "vision_analyze",
        "memory",
        "session_search",
        "web_search",
        "todo",
        "clarify",
    }
)

#: Anything that looks like ``some_tool(`` in the guidance.
CALL_SYNTAX_RE = re.compile(r"\b([a-z_][a-z0-9_]{2,})\(")


def test_guidance_calls_no_tool_that_does_not_exist(corpus: str, ctx):
    """Every call-like token in the corpus must name a real host tool.

    ``register()`` registers no tools, so any call the guidance shows must be
    one the *host* provides. This scans for call syntax generally rather than
    blocklisting a couple of known-bad prefixes, so inventing a brand-new tool
    name fails here instead of shipping.
    """
    from learning_studio import register

    register(ctx)
    assert ctx.tools == [], "this guard assumes this plugin registers no tools"

    called = set(CALL_SYNTAX_RE.findall(corpus))
    unknown = sorted(called - KNOWN_HOST_TOOLS)
    assert unknown == [], (
        f"guidance calls {unknown}, which no registered plugin tool and no known "
        f"host tool provides — the agent would be sent after something that "
        f"cannot answer"
    )


def test_tool_guard_rejects_an_invented_tool_name():
    """Proves the guard above discriminates rather than passing vacuously."""
    invented = 'Call learning_studio_start_exercise("deck") to begin.'

    called = set(CALL_SYNTAX_RE.findall(invented))

    assert sorted(called - KNOWN_HOST_TOOLS) == ["learning_studio_start_exercise"]


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


def test_code_is_allowed_when_it_is_the_subject_matter(references: dict[str, str]):
    """The prohibition must not stop the plugin teaching web subjects.

    Banning HTML/CSS/JS outright would make it impossible to run a CSS
    debugging drill or a JavaScript tracing exercise — a whole class of
    subjects the plugin claims to serve.
    """
    assert_states(
        references.get("manifest-contract", ""),
        (
            r"(subject matter|code as subject)",
            r"(allowed|expected)",
            r"(prompt|answer|source material|feedback)",
        ),
        "code-as-content allowance",
    )


def test_instructional_code_must_stay_inert(references: dict[str, str]):
    """Allowing code as content must not allow it to be executed."""
    assert_states(
        references.get("manifest-contract", ""),
        (
            r"inert",
            r"never (executed|run|mounted)|not executed",
            r"renderer",
        ),
        "inert instructional code",
    )


def test_prohibition_targets_the_renderer_not_the_content(references: dict[str, str]):
    text = normalize(references.get("manifest-contract", ""))
    assert "delivery mechanism" in text or "renderer code" in text, (
        "the prohibition must be scoped to renderer/UI implementation code, "
        "not to code appearing inside an exercise"
    )


# ── Private/public manifest boundary ───────────────────────────────────────


def test_manifest_declares_a_private_public_boundary(references: dict[str, str]):
    assert_states(
        references.get("manifest-contract", ""),
        (
            r"(private|server-side)",
            r"(public|client-side)",
            r"answer[^.]{0,120}(never|not) (be )?sent",
        ),
        "private/public split",
    )


def test_grading_happens_server_side(references: dict[str, str]):
    assert_states(
        references.get("manifest-contract", ""),
        (r"grade on the server", r"(no such thing as a hidden field|readable by the learner)"),
        "server-side grading",
    )


# ── Mastery, timing, and scheduling coherence ──────────────────────────────


def test_meeting_the_standard_retires_the_objective(skill_md: str):
    """Success at the standard must not be read as 'the material is too easy'."""
    assert_states(
        skill_md,
        (
            r"succeeding at the standard",
            r"(retire|maintenance|schedule a review)",
            r"(does )?not license expanding the syllabus|not[^.]{0,60}expand",
        ),
        "mastery stopping rule",
    )


def test_raising_difficulty_is_scoped_to_below_standard_success(skill_md: str):
    assert_states(
        skill_md,
        (r"succeeding below the standard", r"raise the difficulty"),
        "below-standard difficulty rule",
    )


def test_latency_is_only_evidence_when_speed_is_the_objective(skill_md: str):
    assert_states(
        skill_md,
        (
            r"only when speed or automatic recall is part of the stated objective",
            r"(otherwise|not evidence)[^.]{0,120}(not evidence|latency)",
        ),
        "objective-dependent timing",
    )


def test_flashcards_separate_relearning_from_future_scheduling(references: dict[str, str]):
    assert_states(
        references.get("flashcards-and-recall", ""),
        (
            r"relearning step",
            r"next review interval",
            r"after the relearning attempt succeeds",
        ),
        "relearning vs review interval",
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


#: The clauses that make the multi-user warning a warning. Matching on the
#: substance, not on the words "memory" and "learner", which appear throughout
#: SKILL.md for unrelated reasons and would keep this test green after the
#: whole subsection was deleted.
MULTI_USER_CLAUSES = (
    r"(dedicated to (a )?(one|single) learner|single-learner profile)",
    r"(isolated per user|per-user isolation|verified[^.]{0,60}isolat)",
    r"per-user studio storage|studio storage[^.]{0,60}per user",
    r"(profile|memory)[^.]{0,80}(not|never)[^.]{0,40}(to a person|belong to a person)"
    r"|belongs to a \*?profile\*?, not to a person",
)


def strip_multi_user_section(skill_md: str) -> str:
    """SKILL.md with the multi-user warning subsection removed."""
    start = skill_md.index("### Before writing anything learner-specific")
    end = skill_md.index("## Hard rules")
    return skill_md[:start] + skill_md[end:]


def test_multi_user_warning_is_visible_in_the_skill_itself(skill_md: str):
    """A privacy rule buried in a reference file may never be read."""
    assert_states(skill_md, MULTI_USER_CLAUSES, "multi-user warning in SKILL.md")


def test_multi_user_warning_check_fails_without_the_warning(skill_md: str):
    """The discrimination guard.

    If this starts passing, the test above has stopped protecting the privacy
    rule and would survive its deletion — which is exactly how the previous
    version of this check failed review.
    """
    gutted = strip_multi_user_section(skill_md)
    assert len(gutted) < len(skill_md), "fixture no longer removes the section"

    with pytest.raises(AssertionError):
        assert_states(gutted, MULTI_USER_CLAUSES, "multi-user warning in SKILL.md")


# ── Consent before persistence ─────────────────────────────────────────────


def test_inferred_facts_are_confirmed_before_they_are_persisted(skill_md: str):
    assert_states(
        skill_md,
        (r"infer", r"(confirm|correct)[^.]{0,90}(before|durable)"),
        "confirm-before-persist",
    )


def test_sensitive_information_needs_affirmative_permission(skill_md: str):
    assert_states(
        skill_md,
        (r"ask before persisting", r"sensitive", r"accept no as the answer|say no"),
        "affirmative consent for sensitive data",
    )


def test_accessibility_needs_are_session_only_by_default(skill_md: str):
    assert_states(
        skill_md,
        (r"accessibility needs are session-only", r"explicitly asks"),
        "session-only accessibility",
    )


def test_uncertainty_resolves_to_not_persisting(skill_md: str):
    assert_states(
        skill_md,
        (r"(consent|isolation) is uncertain", r"do not persist|uncertainty resolves to no"),
        "uncertainty resolves to no",
    )


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
