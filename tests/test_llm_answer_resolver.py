from __future__ import annotations

import pytest

from job_agent import llm_answer_resolver as resolver_module
from job_agent.llm_answer_resolver import (
    LLMAnswerResolver,
    get_llm_answer_resolver,
    llm_answers_enabled,
    match_screening_rule,
    set_llm_answer_resolver,
)


class FakeLLM:
    def __init__(self, response: str):
        self.response = response
        self.calls: list[list[dict[str, str]]] = []

    def invoke(self, messages: list[dict[str, str]], **kwargs) -> str:
        self.calls.append(messages)
        return self.response


def test_llm_answers_enabled_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setenv("JOB_AGENT_LLM_ANSWERS", "")
    assert not llm_answers_enabled()

    monkeypatch.setenv("JOB_AGENT_LLM_ANSWERS", "1")
    assert llm_answers_enabled()

    monkeypatch.setenv("JOB_AGENT_LLM_ANSWERS", "0")
    assert not llm_answers_enabled()

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("JOB_AGENT_LLM_ANSWERS", "1")
    assert not llm_answers_enabled()


def test_match_screening_rule_matches_substring():
    rules = [
        {"patterns": ["previously worked", "conflict of interest"], "answer": "No"},
        {"patterns": ["willing to relocate"], "answer": "Yes"},
    ]
    assert match_screening_rule("Have you previously worked here?", rules) == "No"
    assert match_screening_rule("Any conflict of interest?", rules) == "No"
    assert match_screening_rule("Willing to relocate?", rules) == "Yes"
    assert match_screening_rule("Favorite color?", rules) is None


def test_match_screening_rule_ignores_empty():
    assert match_screening_rule("anything", []) is None
    assert match_screening_rule("anything", None) is None
    assert match_screening_rule("", [{"patterns": ["x"], "answer": "y"}]) is None


def test_resolver_disabled_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("JOB_AGENT_LLM_ANSWERS", "")
    set_llm_answer_resolver(None)
    assert get_llm_answer_resolver() is None


def test_resolver_returns_validated_option_and_caches():
    fake = FakeLLM('{"answer": "Option B"}')
    resolver = LLMAnswerResolver(llm=fake, max_calls=5)

    field = {
        "kind": "radiogroup",
        "label": "Pick one",
        "options": [
            {"label": "Option A", "autofillId": 1},
            {"label": "Option B", "autofillId": 2},
        ],
    }
    profile = {"target_company": "Acme", "target_title": "SDE"}
    answer = resolver.answer_for_field(field, profile, label="Pick one")

    assert answer == {"label": "Option B", "autofillId": 2}
    assert len(fake.calls) == 1

    # Second call with the same question hits cache.
    answer2 = resolver.answer_for_field(field, profile, label="Pick one")
    assert answer2 == {"label": "Option B", "autofillId": 2}
    assert len(fake.calls) == 1


def test_resolver_validates_multi_select():
    fake = FakeLLM('{"answers": ["Python", "Go"]}')
    resolver = LLMAnswerResolver(llm=fake, max_calls=5)

    field = {
        "kind": "checkboxgroup",
        "label": "Languages",
        "options": [
            {"label": "Python", "autofillId": 1},
            {"label": "Go", "autofillId": 2},
            {"label": "Rust", "autofillId": 3},
        ],
    }
    profile = {"target_company": "Acme", "target_title": "SDE"}
    answer = resolver.answer_for_field(field, profile, label="Languages")

    assert isinstance(answer, list)
    assert len(answer) == 2


def test_resolver_rejects_unknown_option():
    fake = FakeLLM('{"answer": "Not an option"}')
    resolver = LLMAnswerResolver(llm=fake, max_calls=5)

    field = {
        "kind": "select",
        "label": "Degree",
        "options": ["Bachelor", "Master"],
    }
    profile = {"target_company": "Acme", "target_title": "SDE"}
    assert resolver.answer_for_field(field, profile, label="Degree") is None


def test_resolver_returns_short_text_for_free_text():
    fake = FakeLLM('{"answer": "I have built production AI agents."}')
    resolver = LLMAnswerResolver(llm=fake, max_calls=5)

    field = {"kind": "single", "tag": "textarea", "label": "Describe your experience"}
    profile = {"target_company": "Acme", "target_title": "SDE"}
    assert resolver.answer_for_field(field, profile, label="Describe your experience") == "I have built production AI agents."


def test_resolver_rejects_free_text_when_validator_denies():
    fake = FakeLLM('{"answer": "I built AWS data platforms."}')
    resolver = LLMAnswerResolver(
        llm=fake,
        max_calls=5,
        answer_validator=lambda _payload: {
            "verdict": "deny",
            "reason": "AWS is unsupported.",
        },
    )

    field = {"kind": "single", "tag": "textarea", "label": "Describe cloud work"}
    profile = {"target_company": "Acme", "target_title": "SDE"}

    assert resolver.answer_for_field(field, profile, label="Describe cloud work") is None


def test_resolver_rejects_self_validation():
    fake = FakeLLM('{"answer": "I have built production AI agents."}')
    resolver = LLMAnswerResolver(
        llm=fake,
        max_calls=5,
        answer_validator=fake,
    )

    field = {"kind": "single", "tag": "textarea", "label": "Describe your experience"}
    profile = {"target_company": "Acme", "target_title": "SDE"}

    assert resolver.answer_for_field(field, profile, label="Describe your experience") is None


def test_resolver_respects_max_calls():
    fake = FakeLLM('{"answer": "A"}')
    resolver = LLMAnswerResolver(llm=fake, max_calls=1)
    field = {"kind": "radiogroup", "label": "Q", "options": [{"label": "A"}]}
    profile = {"target_company": "Acme", "target_title": "SDE"}

    resolver.answer_for_field(field, profile, label="Q")
    # The second call must not consume a new LLM call because max_calls is reached.
    fake.calls.clear()
    resolver.answer_for_field(field, profile, label="Q2")
    assert len(fake.calls) == 0
