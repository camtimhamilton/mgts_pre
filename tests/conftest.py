"""Normal tests are offline regardless of the developer's shell configuration."""
import pytest


@pytest.fixture(autouse=True)
def offline_provider_default(request, monkeypatch):
    if request.node.get_closest_marker("live") is None:
        monkeypatch.setenv("MGTS_PROVIDER", "mock")
        monkeypatch.delenv("MGTS_ALLOW_SYNTHETIC_LLM", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
