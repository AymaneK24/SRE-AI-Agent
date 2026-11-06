"""Rules for short text that people type and other people later see: names and labels.

Pages show this text with textContent, so it can't run as script there. These rules are a second layer for
everything else that reads it: emails, PDF files, CSV exports, log lines, and API clients.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Annotated

from pydantic import AfterValidator

# Categories: control characters, invisible format characters, and line or paragraph separators.
_BLOCKED_CATEGORIES = {"Cc", "Cf", "Zl", "Zp"}
# A spreadsheet runs a cell that starts with one of these as a formula.
_FORMULA_START = ("=", "+", "-", "@")


def _has_blocked_character(text: str) -> bool:
    return any(unicodedata.category(character) in _BLOCKED_CATEGORIES for character in text)


def clean_label(value: str) -> str:
    """Check a short label, such as a project or source name. Rejects rather than silently changes it."""
    text = unicodedata.normalize("NFKC", value)
    if _has_blocked_character(text):
        raise ValueError("Line breaks and control characters aren't allowed here.")
    if "<" in text or ">" in text:
        raise ValueError("The characters < and > aren't allowed here.")
    text = " ".join(text.split())
    if not text:
        raise ValueError("This can't be empty.")
    if text.startswith(_FORMULA_START):
        raise ValueError("This can't start with =, +, - or @.")
    return text


def clean_person_name(value: str) -> str:
    text = clean_label(value)
    if len(text) < 2:
        raise ValueError("Enter at least 2 characters for the name.")
    return text


def sanitize_stored_name(value: str, fallback: str) -> str:
    """Repair a name that was saved before these rules existed, or that comes from an identity provider."""
    text = unicodedata.normalize("NFKC", value or "")
    text = re.sub(r"<[^>]*>", " ", text)
    text = "".join(character if unicodedata.category(character) not in _BLOCKED_CATEGORIES else " " for character in text)
    text = " ".join(text.replace("<", " ").replace(">", " ").split()).lstrip("=+-@ ")
    return text[:120] if len(text) >= 2 else fallback


def name_from_email(email: str) -> str:
    """A readable first value for the name, until the person changes it."""
    local = email.split("@")[0]
    words = [word for word in re.split(r"[._+-]+", local) if word]
    name = " ".join(word.capitalize() for word in words)
    return name[:120] if len(name) >= 2 else "User"


PersonName = Annotated[str, AfterValidator(clean_person_name)]
Label = Annotated[str, AfterValidator(clean_label)]
