"""Model selection settings and provider construction, per organization."""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from .auth import org_policies
from .config import Config
from .db import Database
from .llm.base import Provider, ProviderError
from .llm.claude import ClaudeProvider
from .llm.gemini import GeminiProvider
from .llm.ollama import OllamaProvider
from .net_guard import BlockedTarget, TargetPolicy, check_url
from .secrets_store import SecretBox

CLAUDE_KEY_SETTING = "claude_api_key_enc"
GEMINI_KEY_SETTING = "gemini_api_key_enc"
CLOUD_PROVIDERS = {"claude": "Anthropic", "gemini": "Google"}


def defaults(config: Config) -> dict[str, Any]:
    return {
        "provider": "ollama",
        "max_rounds": 6,
        "ollama": {"url": config.default_ollama_url, "model": "", "num_ctx": 8192, "use_tools": False, "strict_answers": False, "small_model": "auto"},
        "claude": {"model": "claude-sonnet-5-5", "max_tokens": 4096, "use_tools": True, "consent": False, "strict_answers": True},
        "gemini": {"model": "", "max_tokens": 8192, "use_tools": True, "consent": False, "strict_answers": True},
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


# How investigations keep costs down. Kept apart from the model settings, so older clients that save those keep these.
SAVINGS_SETTING = "ai_savings"


def savings_defaults() -> dict[str, Any]:
    return {
        "prompt_caching": True,       # Claude: reuse the repeated start of each round at about a tenth of the price.
        "gather_evidence": True,      # The console runs the obvious queries itself, before the model starts.
        "answer_style": "short",      # Short answers in the page; automatic investigations always write a full report.
        "evidence_model": {"claude": "", "gemini": ""},  # A cheaper model for the query rounds. Empty: the main model does everything.
    }


async def load_savings(db: Database, org_id: int) -> dict[str, Any]:
    merged = savings_defaults()
    stored = await db.get_setting(org_id, SAVINGS_SETTING, {}) or {}
    for key, value in stored.items():
        if key == "evidence_model" and isinstance(value, dict):
            merged[key].update({name: str(model) for name, model in value.items() if name in merged[key]})
        elif key in merged:
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


async def gemini_key(db: Database, org_id: int, box: SecretBox, config: Config) -> tuple[str, str]:
    """Return (key, source). Like the Claude key, a server-wide key is only used on single-company installations."""
    stored = await db.get_setting(org_id, GEMINI_KEY_SETTING, "")
    if stored:
        return box.decrypt(stored), "saved"
    if config.gemini_api_key and not config.is_hosted:
        return config.gemini_api_key, "environment"
    return "", "none"


async def cloud_key(db: Database, org_id: int, box: SecretBox, config: Config, name: str) -> tuple[str, str]:
    return await (claude_key if name == "claude" else gemini_key)(db, org_id, box, config)


@dataclass
class Selection:
    provider: Provider
    model: str
    use_tools: bool
    context_chars: int
    history_chars: int
    repair: bool = False
    compact: bool = False
    # Cost savings, from load_savings.
    evidence_model: str = ""
    answer_style: str = "short"
    gather_evidence: bool = True


SMALL_MODEL_RE = re.compile(r"[:_\-](\d+(?:\.\d+)?)b\b")


def is_small_model(name: str) -> bool:
    """Models of about 8 billion parameters or fewer, judged from the size in their name, such as qwen2.5:3b."""
    lowered = name.lower()
    if any(word in lowered for word in (":mini", "-mini", ":tiny", "-tiny")):
        return True
    match = SMALL_MODEL_RE.search(lowered)
    return match is not None and float(match.group(1)) <= 8


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
        savings = await load_savings(db, org_id)
        return ClaudeProvider(key, config.anthropic_base_url, int(settings["claude"]["max_tokens"]), bool(savings["prompt_caching"])), settings
    if name == "gemini":
        key, _ = await gemini_key(db, org_id, box, config)
        if not key:
            raise ProviderError("Add a Gemini API key in Connections first.")
        return GeminiProvider(key, config.gemini_base_url, int(settings["gemini"]["max_tokens"])), settings
    raise ProviderError(f"Unknown AI provider: {name}")


async def select(db: Database, org_id: int, box: SecretBox, config: Config, provider_name: str | None, model: str | None, use_tools: bool | None,
                 answer_style: str | None = None) -> Selection:
    provider, settings = await build_provider(db, org_id, box, config, provider_name)
    section = settings[provider.name]
    chosen = model or section.get("model") or ""
    if provider.name in CLOUD_PROVIDERS:
        policies = await org_policies(db, org_id)
        if not policies.get("allow_cloud_ai", True):
            await provider.close()
            raise ProviderError("Your organization's policy doesn't allow sending data to cloud AI models.")
        if not section.get("consent"):
            await provider.close()
            company, label = CLOUD_PROVIDERS[provider.name], provider.name.capitalize()
            raise ProviderError(f"Sending telemetry to {company} is turned off. Turn it on in Connections to use {label}.")
    if not chosen:
        await provider.close()
        raise ProviderError("Choose a model in Connections first.")
    if provider.name == "ollama":
        tokens = int(section["num_ctx"])
        context_chars, history_chars = tokens * 2, tokens
    else:
        context_chars, history_chars = 60000, 60000
    tools = section.get("use_tools", False) if use_tools is None else use_tools
    mode = section.get("small_model", "auto")
    compact = provider.name == "ollama" and (mode == "on" or (mode == "auto" and is_small_model(chosen)))
    savings = await load_savings(db, org_id)
    evidence_model = str(savings["evidence_model"].get(provider.name, "") or "") if provider.name in CLOUD_PROVIDERS else ""
    style = answer_style if answer_style in {"short", "report"} else savings["answer_style"]
    return Selection(provider, chosen, bool(tools), context_chars, history_chars, bool(section.get("strict_answers", False)), compact,
                     evidence_model if evidence_model and evidence_model != chosen else "", style, bool(savings["gather_evidence"]))
