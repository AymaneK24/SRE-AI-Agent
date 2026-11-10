"""Correct the LogQL mistakes models make, so a log search returns evidence instead of a parse error.

Small models in particular write single quotes, put the text they look for in the stream selector
(message='...'), write `| error` instead of `|= "error"`, or leave out the selector. Each fix keeps the
model's intent: the same streams, and the same words.
"""
from __future__ import annotations

import re

# Labels that hold the log line itself in other tools. In Loki the line isn't a label, so these become line filters.
LINE_LABELS = {"message", "msg", "log", "line", "text", "content", "body"}
# Pipeline stages that may follow a |. A bare word that isn't one of these is text to search for.
STAGES = {"json", "logfmt", "regexp", "pattern", "unpack", "line_format", "label_format", "unwrap", "drop", "keep", "decolorize", "label"}
ANY_STREAM = '{job=~".+"}'
STOP_WORDS = {"the", "and", "for", "with", "from", "logs", "log", "lines", "line", "show", "find", "all", "any", "about"}
_QUOTED_SINGLE = re.compile(r"'((?:[^'\\]|\\.)*)'")
_MATCHER = re.compile(r'\s*([A-Za-z_][\w.]*)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"\s*')
_WORDS = re.compile(r"[A-Za-z][\w.-]{2,}")


def _double_quotes(query: str) -> str:
    return _QUOTED_SINGLE.sub(lambda match: '"' + match.group(1).replace('"', '\\"') + '"', query)


def _split_selector(query: str) -> tuple[str, str]:
    """Return (inside the braces, the rest). Without a leading selector the inside is empty and the rest is everything."""
    query = query.strip()
    if not query.startswith("{"):
        return "", query
    depth, quoted = 0, False
    for index, char in enumerate(query):
        if char == '"' and query[index - 1] != "\\":
            quoted = not quoted
        elif not quoted and char == "{":
            depth += 1
        elif not quoted and char == "}":
            depth -= 1
            if depth == 0:
                return query[1:index], query[index + 1:]
    return query[1:], ""


def repair_logql(query: str) -> tuple[str, list[str]]:
    """Return the corrected query and what was changed. An already valid query comes back unchanged."""
    fixes: list[str] = []
    text = query.strip()
    if "'" in text and _QUOTED_SINGLE.search(text):
        text = _double_quotes(text)
        fixes.append("double quotes")
    if not text.startswith("{") and "{" in text:
        # A metric query, such as sum by (job) (count_over_time({...} [5m])). Only the quotes are fixed.
        return (text, fixes) if fixes else (query.strip(), [])
    inside, rest = _split_selector(text)
    line_filters: list[str] = []
    kept: list[str] = []
    if inside.strip():
        for part in [piece for piece in re.split(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", inside) if piece.strip()]:
            match = _MATCHER.fullmatch(part)
            if not match:
                kept.append(part.strip())
                continue
            label, operator, value = match.groups()
            if label.lower() in LINE_LABELS:
                if value and value not in {".*", ".+"}:
                    line_filters.append(f'|~ "(?i){value}"' if operator in {"=~", "="} else f'!~ "(?i){value}"')
                fixes.append(f"{label} is the log line, not a label")
                continue
            kept.append(f'{label}{operator}"{value}"')
    if not text.startswith("{"):
        words = rest.strip()
        rest = ""
        if words:
            if words.startswith("|") or words.startswith("!="):
                rest = words
            else:
                terms = [word for word in _WORDS.findall(words) if word.lower() not in STOP_WORDS][:4]
                line_filters.append(f'|~ "(?i)({"|".join(terms)})"' if terms else '|~ "(?i)(error|exception|fatal|panic)"')
        fixes.append("added a stream selector")
    # Loki needs at least one matcher that can match something, such as job=~".+".
    if not any(re.search(r'(?<![!])(=|=~)"', item) and not re.search(r'=~"\.\*"|="\s*"', item) for item in kept):
        kept.insert(0, 'job=~".+"')
        if inside.strip() and "added a stream selector" not in fixes:
            fixes.append("the selector needs a matcher that selects streams")
    # | error, | "error", and | ~ "x" all mean a line filter.
    def stage(match: re.Match[str]) -> str:
        word = match.group(1)
        if word.lower() in STAGES:
            return match.group(0)
        fixes.append(f'| {word} became |= "{word}"')
        return f' |= "{word}"'
    rest = re.sub(r'\|\s*([A-Za-z_][\w-]*)(?=\s*(?:\||$))', stage, rest)
    rest, count = re.subn(r'\|(?![=~])\s*"', '|= "', rest)
    if count:
        fixes.append('a quoted | became |=')
    rest = re.sub(r'\|\s+([=~])', r'|\1', rest)
    repaired = "{" + ", ".join(kept) + "}" + (" " + " ".join(line_filters) if line_filters else "") + (" " + rest.strip() if rest.strip() else "")
    repaired = re.sub(r"\s+", " ", repaired).strip()
    if repaired == query.strip():
        return query.strip(), []
    return repaired, list(dict.fromkeys(fixes))


def simple_logql(query: str) -> str:
    """A search that always parses: the streams the query named, and its first meaningful word as a filter."""
    inside, _ = _split_selector(_double_quotes(query.strip()))
    kept = []
    for part in inside.split(","):
        match = _MATCHER.fullmatch(part) if part.strip() else None
        if match and match.group(1).lower() not in LINE_LABELS and match.group(2) in {"=", "=~"} and match.group(3) not in {"", ".*"}:
            kept.append(f'{match.group(1)}{match.group(2)}"{match.group(3)}"')
    words = [word for word in _WORDS.findall(query) if word.lower() not in STAGES | LINE_LABELS | {"job", "namespace", "app", "pod", "container"}]
    known = {"error", "exception", "fatal", "panic", "timeout", "failed", "refused", "denied"}
    focus = next((word for word in words if word.lower() in known), None)
    selector = "{" + ", ".join(kept) + "}" if kept else ANY_STREAM
    return f'{selector} |~ "(?i){focus}"' if focus else f'{selector} |~ "(?i)(error|exception|fatal|panic)"'
