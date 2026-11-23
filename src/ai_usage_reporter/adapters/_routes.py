"""Route and provider: the backend a request went through, and the model's maker.

``route`` is the backend as the tool names it (a provider id, a gateway, a
custom profile). It is sent as is and never feeds pricing. ``provider`` is
the model's maker, which the server's price lookup keys on, so it is sent
only when the route says who made the model:

- through a gateway (``openrouter``, ``litellm``) or an unnamed route the
  maker is the model id's own prefix (``anthropic/claude-sonnet-4.6`` gives
  ``anthropic``), or none;
- a local engine (``ollama``, ``lmstudio``…) or a loopback or private base
  URL is ``local``, paid as ``local``;
- a known maker's id (``anthropic``, ``zhipuai``…) is that maker, under the
  name the server uses.

Shared by the adapters whose tools name their backend (Codex, OpenCode, Hermes).
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

#: Routes that serve many makers' models: the provider is the model id's prefix.
GATEWAYS = frozenset({"openrouter", "litellm"})
#: Route names that say nothing about the maker.
NO_PROVIDER = frozenset({"", "custom", "unknown"})
LOCAL_ENGINES = frozenset(
    {"local", "ollama", "lmstudio", "lm-studio", "llamacpp", "llama.cpp", "llama-cpp", "vllm", "mlx", "oss"}
)
#: Provider ids whose maker has another name on the server.
PROVIDER_ALIASES = {
    "openai-codex": "openai",
    "openai-api": "openai",
    "nousresearch": "nous",
    "nous-portal": "nous",
    "google": "gemini",
    "google-gemini": "gemini",
    "google-ai-studio": "gemini",
    "google-vertex": "gemini",
    "vertex": "gemini",
    "vertex-ai": "gemini",
    "vertex_ai": "gemini",
    "vertexai": "gemini",
    "x-ai": "xai",
    "mistralai": "mistral",
    "moonshotai": "moonshot",
    "z-ai": "zai",
    "zhipuai": "zai",
    "qwen": "dashscope",
}
#: Makers the server prices, under the names it uses (after :data:`PROVIDER_ALIASES`).
KNOWN_MAKERS = frozenset(
    {"anthropic", "openai", "gemini", "xai", "deepseek", "mistral", "moonshot", "zai", "dashscope"}
)


def local_url(base_url: str | None) -> bool:
    """Whether *base_url* points at this machine or a private network."""
    host = url_host(base_url)
    if not host:
        return False
    if host == "localhost" or host.endswith((".localhost", ".local")) or host == "host.docker.internal":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def url_host(url: str | None) -> str | None:
    """The host of *url* in lower case, without user, password, port or path (``None``: no host)."""
    if not url or not url.strip():
        return None
    text = url.strip()
    try:
        host = urlsplit(text if "//" in text else f"//{text}").hostname
    except ValueError:
        return None
    return host.lower() if host else None


def alias(name: str) -> str:
    return PROVIDER_ALIASES.get(name, name)


def model_prefix(model: str) -> str | None:
    """The maker a model id names in its prefix (``anthropic/claude-…`` gives ``anthropic``)."""
    prefix = model.split("/", 1)[0].strip().lower() if "/" in model else ""
    return alias(prefix) if prefix else None


def is_local(route: str | None, base_url: str | None = None) -> bool:
    """Whether the route is a local engine, or its base URL is on this machine or a private network."""
    return (route or "").strip().lower() in LOCAL_ENGINES or local_url(base_url)


def resolve_provider(route: str | None, base_url: str | None, model: str) -> tuple[str | None, str | None]:
    """The model's maker and the cost type a route implies (``None``: let the server decide).

    A route that is neither a gateway nor unnamed is taken as the maker's own
    id (Hermes names its backends this way).
    """
    raw = (route or "").strip().lower()
    if is_local(raw, base_url):
        return "local", "local"
    if raw in GATEWAYS or raw in NO_PROVIDER:
        return model_prefix(model), None
    return alias(raw), None


def maker_of(route: str | None, model: str) -> str | None:
    """The model's maker when *route* names it, else ``None`` (the server prices by model name).

    Unlike :func:`resolve_provider`, a route that is not a known maker (a
    bundle, a corporate gateway, a custom profile) gives no provider.
    """
    raw = (route or "").strip().lower()
    if raw in GATEWAYS or raw in NO_PROVIDER:
        return model_prefix(model)
    maker = alias(raw)
    return maker if maker in KNOWN_MAKERS else None
