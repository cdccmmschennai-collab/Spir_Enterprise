"""
Cell-level utilities for reading and cleaning Excel cell values.

Consolidates: clean_str, clean_num, split_tags, placeholder detection.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

# ---------------------------------------------------------------------------
# Separators that indicate multiple tags in one cell
# ---------------------------------------------------------------------------
_TAG_SEPARATORS = re.compile(r"[,/|;]")

# ---------------------------------------------------------------------------
# Placeholder values treated as empty
# ---------------------------------------------------------------------------
PLACEHOLDERS = frozenset(
    {
        "",
        "-",
        "--",
        "---",
        "n/a",
        "na",
        "n.a",
        "n.a.",
        "nil",
        "none",
        "not applicable",
        "not available",
        "unknown",
        ".",
    }
)


def is_placeholder(v: Any) -> bool:
    """Return True if value is None or a recognized placeholder string."""
    if v is None:
        return True
    return str(v).strip().lower() in PLACEHOLDERS


def clean_str(v: Any) -> str | None:
    """Return stripped string or None if placeholder."""
    if is_placeholder(v):
        return None
    return str(v).strip()


def clean_num(v: Any) -> float | None:
    """Return float or None if not numeric / placeholder."""
    if is_placeholder(v):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        pass
    # Try removing currency symbols and commas
    try:
        cleaned = re.sub(r"[^\d.\-]", "", str(v))
        return float(cleaned) if cleaned else None
    except (TypeError, ValueError):
        return None


def split_tags(raw_tag: Any) -> list[str]:
    """
    Split a raw tag cell value into individual tag strings.

    Handles prefix inheritance for various patterns:
        "30-GV-146, 171, 169" -> ["30-GV-146", "30-GV-171", "30-GV-169"]
        "P-3425 A/B"          -> ["P-3425 A", "P-3425 B"]
        "P-3425 A/B/C"        -> ["P-3425 A", "P-3425 B", "P-3425 C"]
        "1/2/3"               -> ["1", "2", "3"]
        "TAG-001"             -> ["TAG-001"]
    """
    if is_placeholder(raw_tag):
        return []

    raw = str(raw_tag).strip()

    # No separator -> single tag
    if not _TAG_SEPARATORS.search(raw):
        return [raw] if raw else []

    parts = [p.strip() for p in _TAG_SEPARATORS.split(raw)]
    parts = [p for p in parts if p and not is_placeholder(p)]

    if not parts:
        return [raw]

    first = parts[0]
    if len(parts) > 1:
        # Case 1: Letter suffix pattern — "P-3425 A/B" or "6834-P-50 A/B"
        # First part ends with " <letter>", remaining parts are single letters
        letter_suffix_match = re.match(r"^(.+\s)([A-Za-z])$", first)
        if letter_suffix_match:
            remaining_are_letters = all(
                re.match(r"^[A-Za-z]$", p) for p in parts[1:]
            )
            if remaining_are_letters:
                base = letter_suffix_match.group(1)  # "P-3425 "
                result = [first]
                for p in parts[1:]:
                    result.append(base + p)
                return result

        # Case 2: Numeric suffix pattern — "30-GV-146, 171, 169"
        prefix_match = re.match(r"^(.*?)(\d+)$", first)
        if prefix_match:
            prefix = prefix_match.group(1)
            result = [first]
            for p in parts[1:]:
                if re.match(r"^\d+$", p) or (
                    not re.search(r"[A-Za-z]", p) and "-" not in p
                ):
                    result.append(prefix + p)
                else:
                    result.append(p)
            return result

    return parts


# Tag pattern used to detect tag-like values in cells
# Default: 1-char minimum prefix to catch V-1234, P-001 style tags
TAG_PATTERN = re.compile(r"\b[A-Z0-9]{1,}[-/][A-Z0-9]", re.IGNORECASE)


@lru_cache(maxsize=1)
def _build_tag_pattern() -> re.Pattern:
    """Build tag pattern from keywords.yaml config, falling back to default."""
    try:
        from spir_dynamic.app.config import load_keywords
        cfg = load_keywords().get("tag_detection", {})
        min_len = cfg.get("min_prefix_length", 1)
        sep = cfg.get("separator_chars", r"[-/]")
        suf = cfg.get("suffix_pattern", r"[A-Z0-9]")
        return re.compile(rf"\b[A-Z0-9]{{{min_len},}}{sep}{suf}", re.IGNORECASE)
    except Exception:
        return TAG_PATTERN


def looks_like_tag(value: Any) -> bool:
    """Return True if value matches a typical equipment tag pattern."""
    if is_placeholder(value):
        return False
    return bool(_build_tag_pattern().search(str(value).strip()))
