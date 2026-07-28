"""Generalized fallback answers for unknown application screening questions.

Two layers keep Greenhouse/Workday (and other ATS) applications hands-free
when a question has no saved profile answer:

1. ``match_screening_rule`` — deterministic, user-authored rules from the
   profile (``screening_answer_rules``: ``[{"patterns": [...], "answer": ...}]``).
   Patterns are company-agnostic substrings (for example "previously worked",
   "conflict of interest"), so one rule generalizes across every employer.
   Because the user writes the rule, the answer stays truthful and approved.
2. ``LLMAnswerResolver`` — a guarded LLM fallback that answers *non-sensitive*
   screening questions from the candidate's own profile facts. It is never
   consulted for sensitive fields (those still require an approved
   sensitive-KB answer), never for user-authored/no-AI questions, and it can
   only pick from the options a control actually offers (validated locally).
   Any failure degrades to the original blocking-review path.

The LLM layer is enabled when ``OPENAI_API_KEY`` is configured; set
``JOB_AGENT_LLM_ANSWERS=0`` to disable it. ``JOB_AGENT_LLM_ANSWERS_MAX_CALLS``
bounds per-run API usage (default 40).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from job_agent.application_answers import _profile_fact_summary
from job_agent.sensitive_kb import normalize

_OPTION_KINDS = {"radiogroup", "buttongroup", "checkboxgroup", "select", "combobox", "button"}
_MULTI_SELECT_KINDS = {"checkboxgroup"}
_ENV_ENABLE_VALUES = {"1", "true", "yes", "on"}
_ENV_DISABLE_VALUES = {"0", "false", "no", "off"}


def llm_answers_enabled(env: dict[str, str] | None = None) -> bool:
    """LLM fallback is disabled by default; enable with ``JOB_AGENT_LLM_ANSWERS=1``."""
    source = os.environ if env is None else env
    raw = str(source.get("JOB_AGENT_LLM_ANSWERS", "")).strip().lower()
    if raw in _ENV_DISABLE_VALUES or raw == "":
        return False
    if raw in _ENV_ENABLE_VALUES:
        return bool(str(source.get("OPENAI_API_KEY", "")).strip())
    return False


def llm_answers_max_calls() -> int:
    raw = str(os.getenv("JOB_AGENT_LLM_ANSWERS_MAX_CALLS", "")).strip()
    try:
        return max(1, min(200, int(raw))) if raw else 40
    except ValueError:
        return 40


def match_screening_rule(label: str, rules: Any) -> str | None:
    """Return the user-approved answer from ``screening_answer_rules``.

    Each rule is ``{"patterns": [...], "answer": "..."}``; a rule matches when
    any normalized pattern is a substring of the normalized label (or vice
    versa for short labels). Rules are the user's explicit standing answer for
    a whole question family, so they apply to any company.
    """
    label_norm = normalize(label)
    if not label_norm or not isinstance(rules, list):
        return None
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        answer = rule.get("answer")
        if answer is None or str(answer).strip() == "":
            continue
        for pattern in rule.get("patterns") or []:
            pattern_norm = normalize(str(pattern))
            if not pattern_norm:
                continue
            if pattern_norm in label_norm or (len(label_norm) >= 12 and label_norm in pattern_norm):
                return str(answer)
    return None


def _norm_token(value: Any) -> str:
    return re.sub(r"\s+", " ", normalize(str(value or ""))).strip()


def _option_text(option: Any) -> str:
    if isinstance(option, dict):
        return str(option.get("label") or option.get("value") or "").strip()
    return str(option or "").strip()


class LLMAnswerResolver:
    """Bounded, cached LLM fallback for non-sensitive screening questions."""

    def __init__(self, llm: Any | None = None, *, max_calls: int | None = None) -> None:
        self._llm = llm
        self._llm_initialized = llm is not None
        self._max_calls = max_calls if max_calls is not None else llm_answers_max_calls()
        self._calls = 0
        self._cache: dict[tuple[Any, ...], str | list[str] | None] = {}

    # -- public API ---------------------------------------------------------

    def answer_for_field(
        self,
        field: dict[str, Any],
        profile: dict[str, Any],
        *,
        label: str | None = None,
    ) -> str | list[str] | None:
        """Return an answer for a non-sensitive field, or None to fall back.

        Option-bearing kinds return a list of validated option labels; free
        text kinds return a short string. Callers remain responsible for the
        sensitive-field gate — never call this for sensitive fields.
        """
        kind = str(field.get("kind") or field.get("tag") or "").strip().lower()
        question = str(label or field.get("label") or "").strip()
        if not question or self._calls >= self._max_calls:
            return None
        option_pairs = [
            (option, _option_text(option))
            for option in field.get("options") or []
            if _option_text(option)
        ]
        options = [text for _raw, text in option_pairs]
        cache_key = (
            _norm_token(question),
            kind,
            tuple(_norm_token(option) for option in options[:40]),
        )
        if cache_key in self._cache:
            return self._cache[cache_key]
        try:
            raw = self._ask(question, kind, options, profile)
        except Exception:
            raw = ""
        answer = self._validate(raw, kind, option_pairs)
        self._cache[cache_key] = answer
        if answer:
            if isinstance(answer, str):
                preview = answer
            elif isinstance(answer, list):
                preview = ", ".join(_option_text(item) for item in answer)
            else:
                preview = _option_text(answer)
            print(f"LLM fallback answer: {question[:90]} -> {str(preview)[:120]}")
        return answer

    # -- internals ----------------------------------------------------------

    def _client(self) -> Any | None:
        if self._llm_initialized:
            return self._llm
        self._llm_initialized = True
        try:
            from hello_agents.core.llm import HelloAgentsLLM

            self._llm = HelloAgentsLLM(
                model=os.getenv("LLM_MODEL_ID") or None,
                temperature=0,
                timeout=45,
            )
        except Exception:
            self._llm = None
        return self._llm

    def _ask(self, question: str, kind: str, options: list[str], profile: dict[str, Any]) -> str:
        llm = self._client()
        if llm is None:
            return ""
        self._calls += 1
        company = str(profile.get("target_company") or "the company")
        title = str(profile.get("target_title") or "this role")
        facts = _profile_fact_summary(profile)
        if kind in _OPTION_KINDS and options:
            numbered = "\n".join(f"{index + 1}. {option}" for index, option in enumerate(options[:40]))
            if kind in _MULTI_SELECT_KINDS:
                instruction = (
                    "Choose EVERY option that truthfully applies (usually just one). "
                    'Reply with ONLY JSON: {"answers": ["<exact option text>", ...]}.'
                )
            else:
                instruction = (
                    "Choose the single most truthful option. "
                    'Reply with ONLY JSON: {"answer": "<exact option text>"}.'
                )
            prompt = (
                "You are completing a job application on behalf of the candidate. "
                "Answer ONLY from the candidate facts below; never invent experience, "
                "credentials, citizenship, clearance, or work authorization. "
                "If the facts cannot support any option, reply with "
                '{"answer": ""} (or {"answers": []}).\n\n'
                f"Company: {company}\nRole: {title}\n\n"
                f"Application question: {question}\n\n"
                f"Available options:\n{numbered}\n\n"
                f"{instruction}\n\n"
                f"Candidate facts:\n{facts}"
            )
        else:
            prompt = (
                "You are completing a job application on behalf of the candidate. "
                "Write a concise, truthful first-person answer to the application "
                "question below, using ONLY the candidate facts provided. Never invent "
                "experience, credentials, citizenship, clearance, or work "
                "authorization. Keep it under 60 words. If the facts cannot support a "
                'truthful answer, reply with {"answer": ""}. '
                'Reply with ONLY JSON: {"answer": "..."}.\n\n'
                f"Company: {company}\nRole: {title}\n\n"
                f"Application question: {question}\n\n"
                f"Candidate facts:\n{facts}"
            )
        response = llm.invoke(
            [{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=300,
        )
        return str(response or "")

    def _validate(
        self,
        raw: str,
        kind: str,
        option_pairs: list[tuple[Any, str]],
    ) -> Any | list[Any] | None:
        payload = _parse_json_object(raw)
        if payload is None:
            return None
        if kind in _MULTI_SELECT_KINDS:
            values = payload.get("answers")
            if not isinstance(values, list):
                single = payload.get("answer")
                values = [single] if isinstance(single, str) and single.strip() else []
            seen: set[int] = set()
            matched: list[Any] = []
            for value in values:
                raw = _match_option(str(value), option_pairs)
                if raw is not None and id(raw) not in seen:
                    seen.add(id(raw))
                    matched.append(raw)
            return matched or None
        value = payload.get("answer")
        if not isinstance(value, str) or not value.strip():
            return None
        value = value.strip()
        if kind in _OPTION_KINDS and option_pairs:
            return _match_option(value, option_pairs)
        return value[:600]


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None


def _match_option(value: str, option_pairs: list[tuple[Any, str]]) -> Any | None:
    """Map an LLM-produced option string back to the original option entry."""
    want = _norm_token(value)
    if not want:
        return None
    for raw, text in option_pairs:
        if _norm_token(text) == want:
            return raw
    # Tolerate small formatting drift, but never match placeholder options.
    for raw, text in option_pairs:
        text_norm = _norm_token(text)
        if text_norm in {"other", "select", "select one", "choose", "none"}:
            continue
        if len(want) >= 3 and (want in text_norm or text_norm in want):
            return raw
    return None


_RESOLVER: LLMAnswerResolver | None = None
_RESOLVER_INITIALIZED = False


def get_llm_answer_resolver() -> LLMAnswerResolver | None:
    """Process-wide resolver used by the runtime; None when disabled."""
    global _RESOLVER, _RESOLVER_INITIALIZED
    if not _RESOLVER_INITIALIZED:
        _RESOLVER_INITIALIZED = True
        _RESOLVER = LLMAnswerResolver() if llm_answers_enabled() else None
    return _RESOLVER


def set_llm_answer_resolver(resolver: LLMAnswerResolver | None) -> None:
    """Test hook: inject a fake resolver (or None to disable)."""
    global _RESOLVER, _RESOLVER_INITIALIZED
    _RESOLVER = resolver
    _RESOLVER_INITIALIZED = True
