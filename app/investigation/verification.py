"""Checks a model's answer against the data it was given, without trusting the model's own claims."""
from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

CONFIDENCE_RE = re.compile(r"confidence\W{0,6}(high|medium|low|unknown)", re.IGNORECASE)
HEADING_RE = re.compile(r"^#{1,4}\s*(.+?)\s*$", re.MULTILINE)
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
NUMBER_RE = re.compile(
    r"(?<![\w./:\-])(-?\d[\d,]*(?:\.\d+)?)\s*(%|percent|ms|µs|seconds?|secs?|s\b|GiB|MiB|KiB|GB|MB|KB|req/s|/s)?(?![\w:.\-]*\d)",
)
CORPUS_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?")
DIAGNOSIS_WORDS = re.compile(
    r"\b(why|cause|root|debug|investigat\w*|wrong|fail\w*|down|broken|slow|error\w*|issue|problem|incident|outage|pourquoi|cause|panne|probl[eè]me|erreur)\b",
    re.IGNORECASE,
)
REQUIRED_SECTIONS = ("Likely root cause", "Evidence", "Competing explanation")
# Text that only appears when a query failed. Quoted as evidence, it means the model used an error as if it were data.
ERROR_TEXT_RE = re.compile(r"invalid parameter|parse error|query error|unknown tool|unexpected character|bad_data|invalid arguments", re.IGNORECASE)
LOG_QUERY_RE = re.compile(r"\|[=~]|!=\s*\"|\|\s*json|\|\s*logfmt|\bwhere\b|\bproject\b|\bsummarize\b")


def _problem_terms(problem: dict[str, Any]) -> set[str]:
    """Words that show an answer talks about this problem: the workload, its pods, the alert, or the target."""
    detail = problem.get("detail") or {}
    terms = set()
    for key in ("workload", "name", "instance", "job"):
        value = str(detail.get(key) or "").strip()
        if len(value) >= 3:
            terms.add(value.lower())
    for pod in detail.get("pods") or []:
        terms.add(str(pod).lower())
    if problem.get("kind") == "signal" and detail.get("title"):
        terms.add(str(detail["title"]).lower())
    return terms


def missed_problems(answer: str, problems: list[dict[str, Any]]) -> list[str]:
    """Problems the console sees that the answer never names. Only critical and warning problems count."""
    lowered = answer.lower()
    missed = []
    for problem in problems:
        if problem.get("severity") not in {"critical", "warning"}:
            continue
        terms = _problem_terms(problem)
        if terms and not any(term in lowered for term in terms):
            missed.append(problem["title"])
    return missed


def expects_diagnosis(prompt: str, answer: str) -> bool:
    return bool(DIAGNOSIS_WORDS.search(prompt)) or "root cause" in answer.lower()


def _sections(answer: str) -> dict[str, str]:
    matches = list(HEADING_RE.finditer(answer))
    result: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(answer)
        result[match.group(1).strip().lower()] = answer[match.end():end].strip()
    return result


def _section(sections: dict[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in sections.items():
        if wanted in key:
            return value
    return None


def missing_sections(answer: str) -> list[str]:
    sections = _sections(answer)
    missing = [name for name in REQUIRED_SECTIONS if _section(sections, name) is None]
    if not CONFIDENCE_RE.search(answer):
        missing.append("a Confidence line")
    return missing


def _float(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def corpus_numbers(texts: list[str]) -> list[float]:
    found: set[float] = set()
    for text in texts:
        for match in CORPUS_NUMBER_RE.findall(text):
            value = _float(match)
            if value is not None:
                found.add(value)
    return sorted(found)


def _candidates(value: float, unit: str) -> list[float]:
    unit = (unit or "").lower()
    options = [value]
    if unit in {"ms"}:
        options += [value / 1000]
    elif unit in {"µs"}:
        options += [value / 1_000_000]
    elif unit in {"s", "sec", "secs", "second", "seconds"}:
        options += [value * 1000]
    elif unit in {"%", "percent"}:
        options += [value / 100]
    elif unit in {"gib", "gb"}:
        options += [value * 2**30, value * 1e9]
    elif unit in {"mib", "mb"}:
        options += [value * 2**20, value * 1e6]
    elif unit in {"kib", "kb"}:
        options += [value * 2**10, value * 1e3]
    return options


def _matches(raw: str, value: float, unit: str, corpus: list[float]) -> bool:
    decimals = len(raw.split(".")[1]) if "." in raw else 0
    display_tolerance = 0.51 * 10 ** (-decimals)
    for candidate in _candidates(value, unit):
        scale = abs(candidate / value) if value else 1.0
        tolerance = max(display_tolerance * scale, abs(candidate) * 0.005)
        for known in corpus:
            if abs(known - candidate) <= tolerance:
                return True
    return False


def check_numbers(text: str, corpus: list[float]) -> dict[str, Any]:
    """Compare numbers cited in prose with numbers present in the evidence."""
    cleaned = INLINE_CODE_RE.sub(" ", FENCE_RE.sub(" ", text))
    checked, unmatched = 0, []
    for match in NUMBER_RE.finditer(cleaned):
        raw, unit = match.group(1), match.group(2) or ""
        value = _float(raw)
        if value is None:
            continue
        if not unit and "." not in raw and abs(value) <= 10:
            continue  # Small counts such as "2 targets" appear in prose too often to check.
        checked += 1
        if not _matches(raw.replace(",", ""), value, unit, corpus):
            unmatched.append(f"{raw}{(' ' + unit) if unit else ''}")
    return {"checked": checked, "matched": checked - len(unmatched), "unmatched": unmatched[:10]}


def cited_queries(text: str) -> list[str]:
    output = []
    for code in INLINE_CODE_RE.findall(text):
        code = code.strip()
        if LOG_QUERY_RE.search(code):
            continue  # A log query can't be checked against a metric source.
        if ("(" in code or "{" in code or "|" in code or "_" in code) and len(code) > 2 and code not in output:
            output.append(code)
    return output[:8]


QueryRunner = Callable[[str], Awaitable[dict[str, Any]]]


async def verify_answer(prompt: str, answer: str, evidence_texts: list[str], run_query: QueryRunner | None,
                        problems: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if not expects_diagnosis(prompt, answer):
        return {"kind": "direct", "confidence_final": None}
    sections = _sections(answer)
    match = CONFIDENCE_RE.search(_section(sections, "Likely root cause") or answer)
    stated = match.group(1).capitalize() if match else None
    evidence = _section(sections, "Evidence") or ""
    root = _section(sections, "Likely root cause") or ""
    numbers = check_numbers(f"{root}\n{evidence}", corpus_numbers(evidence_texts))

    queries = {"checked": 0, "valid": 0, "empty": 0, "invalid": []}
    if run_query:
        for query in cited_queries(evidence):
            queries["checked"] += 1
            try:
                result = await run_query(query)
                queries["valid"] += 1
                if not result.get("result"):
                    queries["empty"] += 1
            except Exception as error:  # Any failure means the cited query doesn't support the claim.
                queries["invalid"].append({"query": query[:200], "error": str(getattr(error, "message", error))[:160]})

    has_competing = _section(sections, "Competing explanation") is not None
    reasons = []
    if not has_competing:
        reasons.append("No competing explanation was given.")
    if not evidence:
        reasons.append("No evidence section was given.")
    if numbers["checked"] and len(numbers["unmatched"]) / numbers["checked"] > 0.25:
        reasons.append(f"{len(numbers['unmatched'])} of {numbers['checked']} cited numbers don't match the data.")
    if queries["invalid"]:
        reasons.append(f"{len(queries['invalid'])} cited queries failed when run.")
    if ERROR_TEXT_RE.search(evidence):
        reasons.append("The evidence quotes a failed query's error message as if it were data.")
    missed = missed_problems(answer, problems or [])
    if missed:
        shown = "; ".join(missed[:3]) + (f"; and {len(missed) - 3} more" if len(missed) > 3 else "")
        reasons.append(f"The answer doesn't address what the console sees as broken right now: {shown}.")
    final = stated
    if stated in {"High", "Medium"} and reasons:
        final = "Low"
    if stated is None:
        final = "Not stated"
    return {
        "kind": "diagnosis", "confidence_stated": stated, "confidence_final": final, "lowered": final != stated and stated is not None,
        "reasons": reasons, "numbers": numbers, "queries": queries, "has_competing_explanation": has_competing,
        "missed_problems": missed,
    }
