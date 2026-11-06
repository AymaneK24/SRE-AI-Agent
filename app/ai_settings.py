"""Model selection settings and provider construction, per organization."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .auth import org_policies
from .config import Config
from .db import Database
from .llm.base import Provider, ProviderError
from .llm.claude import ClaudeProvider
from .llm.ollama import OllamaProvider
from .net_guard import BlockedTarget, TargetPolicy, check_url
from .secrets_store import SecretBox

CLAUDE_KEY_SETTING = "claude_api_key_enc"


def defaults(config: Config) -> dict[str, Any]:
    return {
        "provider": "ollama",
        "max_rounds": 6,
        "ollama": {"url": config.default_ollama_url, "model": "", "num_ctx": 8192, "use_tools": False, "strict_answers": False},
        "claude": {"model": "claude-sonnet-5-5", "max_tokens": 4096, "use_tools": True, "consent": False, "strict_answers": True},
    }


async def load_settings(db: Database, org_id: int, config: Config) -> dict[str, Any]:
    merged = copy.deepcopy(defaults(config))
    stored = await db.get_setting(org_id, "ai", {}) or {}
    for key, value in stored.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


async def claude_key(db: Database, org_id: int, box: SecretBox, config: Config) -> tuple[str, str]:
    """Return (key, source). A server-wide key is only used on single-company installations."""
    stored = await db.get_setting(org_id, CLAUDE_KEY_SETTING, "")
    if stored:
        return box.decrypt(stored), "saved"
    if config.anthropic_api_key and not config.is_hosted:
        return config.anthropic_api_key, "environment"
    return "", "none"


@dataclass
class Selection:
    provider: Provider
    model: str
    use_tools: bool
    context_chars: int
    history_chars: int
    repair: bool = False


async def build_provider(db: Database, org_id: int, box: SecretBox, config: Config, name: str | None = None) -> tuple[Provider, dict[str, Any]]:
    settings = await load_settings(db, org_id, config)
    name = name or settings["provider"]
    if name == "ollama":
        section = settings["ollama"]
        try:
            if section["url"] != config.default_ollama_url:
                await check_url(section["url"], config.target_policy)
        except BlockedTarget as error:
            raise ProviderError(str(error)) from error
        # The address in RCC_OLLAMA_URL is set by whoever runs the server, so it may be on the same machine.
        policy = TargetPolicy(allow_loopback=True) if section["url"] == config.default_ollama_url else config.target_policy
        return OllamaProvider(section["url"], int(section["num_ctx"]), config.in_container, policy), settings
    if name == "claude":
        key, _ = await claude_key(db, org_id, box, config)
        if not key:
            raise ProviderError("Add a Claude API key in Connections first.")
        return ClaudeProvider(key, config.anthropic_base_url, int(settings["claude"]["max_tokens"])), settings
    raise ProviderError(f"Unknown AI provider: {name}")


async def select(db: Database, org_id: int, box: SecretBox, config: Config, provider_name: str | None, model: str | None, use_tools: bool | None) -> Selection:
    provider, settings = await build_provider(db, org_id, box, config, provider_name)
    section = settings[provider.name]
    chosen = model or section.get("model") or ""
    if provider.name == "claude":
        policies = await org_policies(db, org_id)
        if not policies.get("allow_cloud_ai", True):
            await provider.close()
            raise ProviderError("Your organization's policy doesn't allow sending data to cloud AI models.")
        if not section.get("consent"):
            await provider.close()
            raise ProviderError("Sending telemetry to Anthropic is turned off. Turn it on in Connections to use Claude.")
    if not chosen:
        await provider.close()
        raise ProviderError("Choose a model in Connections first.")
    if provider.name == "ollama":
        tokens = int(section["num_ctx"])
        context_chars, history_chars = tokens * 2, tokens
    else:
        context_chars, history_chars = 60000, 60000
    tools = section.get("use_tools", False) if use_tools is None else use_tools
    return Selection(provider, chosen, bool(tools), context_chars, history_chars, bool(section.get("strict_answers", False)))
