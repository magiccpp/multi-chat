from app import crypto
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import Provider
from app.providers.mock import MockProvider
from app.providers.openai_compat import OpenAICompatProvider
from app.tables import Endpoint

_KINDS: dict[str, type[Provider]] = {
    "openai_compatible": OpenAICompatProvider,
    "anthropic": AnthropicProvider,
    "mock": MockProvider,
}

# Presets offered by the clients when adding an endpoint.
PRESETS = [
    {"id": "openai", "name": "OpenAI", "kind": "openai_compatible", "base_url": "https://api.openai.com/v1"},
    {"id": "anthropic", "name": "Anthropic", "kind": "anthropic", "base_url": None},
    {"id": "openrouter", "name": "OpenRouter", "kind": "openai_compatible", "base_url": "https://openrouter.ai/api/v1"},
    {"id": "deepseek", "name": "DeepSeek", "kind": "openai_compatible", "base_url": "https://api.deepseek.com"},
    {
        "id": "gemini",
        "name": "Google Gemini",
        "kind": "openai_compatible",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    },
    {"id": "xai", "name": "xAI", "kind": "openai_compatible", "base_url": "https://api.x.ai/v1"},
    {"id": "groq", "name": "Groq", "kind": "openai_compatible", "base_url": "https://api.groq.com/openai/v1"},
    {"id": "mistral", "name": "Mistral", "kind": "openai_compatible", "base_url": "https://api.mistral.ai/v1"},
    {"id": "ollama", "name": "Ollama", "kind": "openai_compatible", "base_url": "http://localhost:11434/v1"},
    {"id": "claude_code", "name": "Claude Code (CLI)", "kind": "agent_cli", "driver": "claude_code", "base_url": None},
    {"id": "cursor", "name": "Cursor (CLI)", "kind": "agent_cli", "driver": "cursor", "base_url": None},
    {"id": "copilot", "name": "GitHub Copilot (CLI)", "kind": "agent_cli", "driver": "copilot", "base_url": None},
    {
        "id": "github_models",
        "name": "GitHub Models",
        "kind": "openai_compatible",
        "base_url": "https://models.github.ai/inference",
    },
    {"id": "mock", "name": "Mock (offline demo)", "kind": "mock", "base_url": "mock://local?delay=0.02"},
]

_cache: dict[tuple[int, str | None, str | None], Provider] = {}


def provider_for(endpoint: Endpoint) -> Provider:
    if endpoint.kind not in _KINDS:
        raise ValueError(f"endpoint kind {endpoint.kind!r} is not a chat-completion provider")
    key = (endpoint.id, endpoint.base_url, endpoint.api_key_enc)
    if key not in _cache:
        _cache[key] = _KINDS[endpoint.kind](api_key=crypto.decrypt(endpoint.api_key_enc), base_url=endpoint.base_url)
    return _cache[key]
