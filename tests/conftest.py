import pytest


@pytest.fixture(autouse=True)
def isolate_resume_source_env(monkeypatch):
    # Local .env may point at a real resume directory. Tests should opt into
    # that guard explicitly instead of inheriting the developer machine state.
    monkeypatch.setenv("RESUME_SOURCE_DIR", "")
