from __future__ import annotations

import pytest

from job_agent import llm_answer_resolver as resolver_module
from job_agent.llm_answer_resolver import LLMAnswerResolver, set_llm_answer_resolver
from job_agent.python_runtime import _plan_field


class FakeLLM:
    def __init__(self, response: str):
        self.response = response
        self.calls: list[list[dict[str, str]]] = []

    def invoke(self, messages: list[dict[str, str]], **kwargs) -> str:
        self.calls.append(messages)
        return self.response


@pytest.fixture(autouse=True)
def reset_resolver():
    """Ensure each test starts with no resolver unless explicitly injected."""
    set_llm_answer_resolver(None)
    yield
    set_llm_answer_resolver(None)


def test_plan_field_uses_llm_for_unmapped_required_radio():
    option_b = {"label": "No", "autofillId": "no-1"}
    field = {
        "kind": "radiogroup",
        "label": "Have you previously worked at Acme?",
        "required": True,
        "options": [{"label": "Yes", "autofillId": "yes-1"}, option_b],
    }
    profile = {
        "target_company": "Acme",
        "target_title": "SDE",
        "work_history": [],
        "education": [],
        "skills": [],
        "projects": [],
    }
    resolver = LLMAnswerResolver(llm=FakeLLM('{"answer": "No"}'), max_calls=5)
    set_llm_answer_resolver(resolver)

    plan = _plan_field(field, profile, None)
    assert plan["action"] == "check"
    assert plan["option"] == option_b


def test_plan_field_uses_llm_for_unmapped_required_text():
    field = {
        "kind": "single",
        "tag": "textarea",
        "label": "What exceptional work have you done?",
        "required": True,
    }
    profile = {
        "target_company": "Acme",
        "target_title": "SDE",
        "work_history": [{"title": "MLE", "company": "X"}],
        "education": [],
        "skills": ["Python"],
        "projects": [],
    }
    resolver = LLMAnswerResolver(
        llm=FakeLLM('{"answer": "I built a real-time fraud detection pipeline at X."}'),
        max_calls=5,
    )
    set_llm_answer_resolver(resolver)

    plan = _plan_field(field, profile, None)
    assert plan["action"] == "fill"
    assert "pipeline" in plan["value"]


def test_plan_field_prefers_screening_rule_over_llm():
    field = {
        "kind": "radiogroup",
        "label": "Any conflict of interest?",
        "required": True,
        "options": [{"label": "Yes"}, {"label": "No", "autofillId": "no"}],
    }
    profile = {
        "screening_answer_rules": [
            {"patterns": ["conflict of interest"], "answer": "No"}
        ],
        "work_history": [],
        "education": [],
        "skills": [],
        "projects": [],
    }
    fake_llm = FakeLLM('{"answer": "Yes"}')
    set_llm_answer_resolver(LLMAnswerResolver(llm=fake_llm, max_calls=5))

    plan = _plan_field(field, profile, None)
    assert plan["action"] == "check"
    assert plan["option"] == {"label": "No", "autofillId": "no"}
    assert len(fake_llm.calls) == 0


def test_plan_field_does_not_call_llm_for_sensitive_fields():
    field = {
        "kind": "radiogroup",
        "label": "Will you now or in the future require sponsorship?",
        "required": True,
        "options": [{"label": "Yes"}, {"label": "No"}],
    }
    profile = {
        "target_company": "Acme",
        "target_title": "SDE",
        "work_history": [],
        "education": [],
        "skills": [],
        "projects": [],
    }
    fake_llm = FakeLLM('{"answer": "No"}')
    set_llm_answer_resolver(LLMAnswerResolver(llm=fake_llm, max_calls=5))

    plan = _plan_field(field, profile, None)
    assert plan["action"] == "skip"
    assert plan["sensitive"] is True
    assert len(fake_llm.calls) == 0


def test_plan_field_does_not_call_llm_for_user_authored_instruction():
    field = {
        "kind": "single",
        "tag": "textarea",
        "label": "Why this role?",
        "required": True,
    }
    profile = {
        "application_requires_user_authored_answers": True,
        "target_company": "Acme",
        "target_title": "SDE",
        "work_history": [],
        "education": [],
        "skills": [],
        "projects": [],
    }
    fake_llm = FakeLLM('{"answer": "AI wrote this"}')
    set_llm_answer_resolver(LLMAnswerResolver(llm=fake_llm, max_calls=5))

    plan = _plan_field(field, profile, None)
    assert plan["action"] == "skip"
    assert "user-authored" in plan["reason"]
    assert len(fake_llm.calls) == 0


def test_plan_field_blocks_when_llm_returns_no_matching_option():
    field = {
        "kind": "radiogroup",
        "label": "Pick a number",
        "required": True,
        "options": [{"label": "One"}, {"label": "Two"}],
    }
    profile = {"target_company": "Acme", "target_title": "SDE"}
    set_llm_answer_resolver(
        LLMAnswerResolver(llm=FakeLLM('{"answer": "Three"}'), max_calls=5)
    )

    plan = _plan_field(field, profile, None)
    assert plan["action"] == "skip"
