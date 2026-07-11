from job_agent.resume_plans import (
    propose_resume_edit_plan,
    render_llm_tailored_resume_draft,
    render_tailored_resume_draft,
)


class FakeLLM:
    provider = "openai"

    def __init__(self, output: str = ""):
        self.output = output
        self.last_messages = None

    def invoke(self, messages, **kwargs):
        self.last_messages = messages
        return self.output


BASE_RESUME = "Gaoyi Wu\n\nBuilt Python and FastAPI services."
JD_WITH_RUST = "Title: Agent Engineer\n\nBuild LangChain agents with FastAPI and Rust."
JD_CLEAN = "Title: Agent Engineer\n\nBuild LangChain agents with FastAPI."


def test_llm_tailored_resume_uses_llm_output_with_header_and_review():
    llm = FakeLLM(output="# Gaoyi Wu\n\nTargeted summary emphasizing LangChain and FastAPI.")
    draft = render_llm_tailored_resume_draft(BASE_RESUME, JD_CLEAN, llm)

    assert "# Tailored Resume Draft (LLM)" in draft
    assert "Targeted summary emphasizing LangChain and FastAPI." in draft
    assert "Truthfulness Review (LLM draft)" in draft
    # the LLM was actually called with the grounded system prompt
    assert llm.last_messages is not None
    assert "never invent" in llm.last_messages[0]["content"].lower()


def test_llm_tailored_resume_flags_leaked_unsupported_keywords():
    # LLM "invents" Rust (unsupported) -> truthfulness gate must flag it
    llm = FakeLLM(output="# Gaoyi Wu\n\nAdded Rust and LangChain experience.")
    draft = render_llm_tailored_resume_draft(BASE_RESUME, JD_WITH_RUST, llm)

    assert "Rust" in draft
    assert "WARNING" in draft


def test_llm_tailored_resume_falls_back_to_deterministic_when_llm_empty():
    llm = FakeLLM(output="")
    draft = render_llm_tailored_resume_draft(BASE_RESUME, JD_CLEAN, llm)

    # deterministic fallback header has no "(LLM)" suffix
    assert draft.startswith("# Tailored Resume Draft\n")
    assert "(LLM)" not in draft


def test_llm_tailored_resume_falls_back_when_llm_raises():
    class RaisingLLM:
        provider = "openai"

        def invoke(self, messages, **kwargs):
            raise RuntimeError("boom")

    draft = render_llm_tailored_resume_draft(BASE_RESUME, JD_CLEAN, RaisingLLM())

    assert "(LLM)" not in draft
    assert "Tailored Resume Draft" in draft
