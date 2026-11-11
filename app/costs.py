"""What each investigation costs, monthly forecasts, and budget limits."""
from __future__ import annotations

import calendar
from datetime import datetime, timezone
from typing import Any

from .db import Database

def month_start() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# US dollars per million tokens, from Anthropic's published pricing as of 2026-11-04.
# Each organization can override these, because prices change.
DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "claude-fable-5-1": {"input": 10.0, "output": 50.0},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50},
    # Google's paid-tier text prices, from https://ai.google.dev/gemini-api/docs/pricing as read on 2026-11-07.
    # Output includes thinking tokens, which this console counts as output.
    "gemini-3.5-flash": {"input": 1.50, "output": 9.00},
    "gemini-3-flash-preview": {"input": 0.50, "output": 3.00},
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50},
    "gemini-3.1-pro-preview": {"input": 2.00, "output": 12.00},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
}
# Prompts longer than "over" tokens are billed at a higher rate for these models.
LONG_PROMPT: dict[str, dict[str, float]] = {
    "claude-haiku-5-5": {"over": 100_000, "input": 0.50, "output": 2.50},
    "gemini-3.1-pro-preview": {"over": 200_000, "input": 4.00, "output": 18.00},
    "gemini-2.5-pro": {"over": 200_000, "input": 2.50, "output": 15.00},
}


# Cached prompt tokens are billed as a share of the input price. Anthropic: reads 10%, writes (5-minute cache) 125%.
# Google's implicit caching: reads at a discount of at least 75% on Gemini 2.5 and later, so 25% is used as a safe estimate.
CACHE_READ_RATE = {"claude": 0.10, "gemini": 0.25}
CACHE_WRITE_RATE = {"claude": 1.25, "gemini": 1.0}


def _price(model: str, input_tokens: int, prices: dict[str, Any]) -> dict[str, Any] | None:
    price = prices.get(model) or DEFAULT_PRICES.get(model)
    long_prompt = LONG_PROMPT.get(model)
    if price and long_prompt and model not in prices and input_tokens > long_prompt["over"]:
        price = long_prompt
    return price


def compute_cost(provider: str, model: str, input_tokens: int | None, output_tokens: int | None, prices: dict[str, Any],
                 cache_read: int = 0, cache_write: int = 0) -> float | None:
    """Return the cost in dollars, or None when no price is known for this model. input_tokens includes cached tokens."""
    if provider == "ollama":
        return 0.0
    if input_tokens is None:
        return None
    price = _price(model, input_tokens, prices)
    if not price:
        return None
    cache_read, cache_write = min(cache_read, input_tokens), min(cache_write, max(input_tokens - cache_read, 0))
    plain = input_tokens - cache_read - cache_write
    rate = float(price["input"])
    billed = (plain + cache_read * CACHE_READ_RATE.get(provider, 1.0) + cache_write * CACHE_WRITE_RATE.get(provider, 1.0)) * rate
    return (billed + (output_tokens or 0) * float(price["output"])) / 1_000_000


def cache_saving(provider: str, model: str, input_tokens: int | None, output_tokens: int | None, prices: dict[str, Any],
                 cache_read: int = 0, cache_write: int = 0) -> float | None:
    """What prompt caching saved on one call, compared with sending everything at the full input price."""
    actual = compute_cost(provider, model, input_tokens, output_tokens, prices, cache_read, cache_write)
    full = compute_cost(provider, model, input_tokens, output_tokens, prices)
    if actual is None or full is None:
        return None
    return round(full - actual, 6)


async def spend_settings(db: Database, org_id: int) -> dict[str, Any]:
    stored = await db.get_setting(org_id, "spend", {}) or {}
    return {"monthly_budget_usd": None, "max_tokens_per_investigation": None, "prices": {}, **stored}


async def month_spend(db: Database, org_id: int) -> float:
    row = await db.get("SELECT COALESCE(SUM(cost_usd), 0) AS total FROM llm_calls WHERE org_id = ? AND started_at >= ?", (org_id, month_start()))
    return float(row["total"])


async def budget_problem(db: Database, org_id: int) -> str | None:
    settings = await spend_settings(db, org_id)
    budget = settings["monthly_budget_usd"]
    if budget is not None and await month_spend(db, org_id) >= float(budget):
        return f"This organization has reached its monthly AI budget of ${float(budget):.2f}. An admin can raise it in Organization."
    return None


async def usage_summary(db: Database, org_id: int) -> dict[str, Any]:
    start = month_start()
    settings = await spend_settings(db, org_id)
    rows = await db.all(
        "SELECT provider, model, count(*) AS calls, COALESCE(SUM(input_tokens), 0) AS input_tokens, COALESCE(SUM(output_tokens), 0) AS output_tokens, "
        "COALESCE(SUM(cost_usd), 0) AS cost_usd, COALESCE(SUM(duration_ms), 0) AS duration_ms, "
        "SUM(CASE WHEN cost_usd IS NULL AND provider <> 'ollama' AND status = 'complete' THEN 1 ELSE 0 END) AS unpriced_calls "
        "FROM llm_calls WHERE org_id = ? AND started_at >= ? GROUP BY provider, model ORDER BY cost_usd DESC", (org_id, start))
    investigations = await db.get(
        "SELECT count(*) AS n, COALESCE(AVG(m.cost_usd), 0) AS avg_cost, COALESCE(AVG(m.duration_ms), 0) AS avg_ms FROM messages m JOIN conversations c ON c.id = m.conversation_id "
        "WHERE c.org_id = ? AND m.role = 'assistant' AND m.status = 'complete' AND m.created_at >= ?", (org_id, start))
    spent = sum(float(row["cost_usd"]) for row in rows)
    for row in rows:
        # A model with no known price shows as unknown, not as free.
        if row["provider"] != "ollama" and int(row["unpriced_calls"] or 0) > 0 and float(row["cost_usd"]) == 0:
            row["cost_usd"] = None
    now = datetime.now(timezone.utc)
    days = calendar.monthrange(now.year, now.month)[1]
    elapsed = max((now - now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 86400, 1.0)
    budget = settings["monthly_budget_usd"]
    return {
        "month_start": start,
        "spent_usd": round(spent, 4),
        "forecast_usd": round(spent / elapsed * days, 2),
        "budget_usd": budget,
        "investigations": investigations["n"],
        "avg_cost_usd": round(float(investigations["avg_cost"]), 4),
        "avg_duration_s": round(float(investigations["avg_ms"]) / 1000, 1),
        "by_model": rows,
        "max_tokens_per_investigation": settings["max_tokens_per_investigation"],
        "prices": {**DEFAULT_PRICES, **settings["prices"]},
    }
