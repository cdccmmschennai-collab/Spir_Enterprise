"""
extraction/registry/format_registry.py
────────────────────────────────────────
Format Registry — pluggable, priority-ordered handler list.

ADDING A NEW FORMAT IN 3 STEPS:
    1. Create extraction/formats/format9.py
       def detect(wb) -> bool: ...
       def extract(wb) -> FormatResult: ...

    2. Add one entry to REGISTRY_CONFIG in bootstrap.py:
       ("FORMAT9", 15, "Description of new format")

    3. Done. No other files change.

DESIGN PRINCIPLES:
    • FormatEntry is a frozen dataclass — immutable after creation.
    • FormatRegistry holds a sorted list — no dict, no global mutation after bootstrap.
    • Thread-safe by construction (list is sorted once at bootstrap time).
    • dispatch() returns FormatResult | None — caller handles fallback.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, runtime_checkable

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Output contract — every extractor returns this
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FormatResult:
    format_name:     str
    spir_no:         str
    equipment:       str
    manufacturer:    str
    supplier:        str
    spir_type:       Optional[str]
    eqpt_qty:        int
    spare_items:     int
    total_tags:      int
    annexure_count:  int
    annexure_stats:  dict[str, int]
    rows:            list[list[Any]]

    def is_empty(self) -> bool:
        return len(self.rows) == 0

    def tag_set(self, tag_ci: int) -> set:
        return {r[tag_ci] for r in self.rows if r[tag_ci] is not None}

    def sheet_set(self, sheet_ci: int) -> set:
        return {r[sheet_ci] for r in self.rows if r[sheet_ci] is not None}


# ─────────────────────────────────────────────────────────────────────────────
# Protocols
# ─────────────────────────────────────────────────────────────────────────────

@runtime_checkable
class Detector(Protocol):
    def __call__(self, wb: Any) -> bool: ...

@runtime_checkable
class Extractor(Protocol):
    def __call__(self, wb: Any) -> FormatResult: ...


# ─────────────────────────────────────────────────────────────────────────────
# Registry entry
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FormatEntry:
    name:        str
    description: str
    detect:      Detector
    extract:     Extractor
    priority:    int = 50   # lower = checked first


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────

class FormatRegistry:
    """
    Ordered, thread-safe registry.  Sorted by priority at registration time.
    """

    def __init__(self) -> None:
        self._entries: list[FormatEntry] = []

    def register(self, entry: FormatEntry) -> None:
        self._entries.append(entry)
        self._entries.sort(key=lambda e: e.priority)

    def detect(self, wb: Any) -> Optional[FormatEntry]:
        for entry in self._entries:
            try:
                if entry.detect(wb):
                    return entry
            except Exception as exc:
                log.debug("Format %s detect() error: %s", entry.name, exc)
        return None

    def dispatch(self, wb: Any) -> Optional[FormatResult]:
        entry = self.detect(wb)
        if entry is None:
            return None
        try:
            result = entry.extract(wb)
            if result.is_empty():
                log.warning("Format %s returned 0 rows", entry.name)
                return None
            log.info("Known engine: format=%s rows=%d", entry.name, len(result.rows))
            return result
        except Exception as exc:
            log.error("Format %s extract() failed: %s", entry.name, exc, exc_info=True)
            return None

    def list_all(self) -> list[dict]:
        return [{"name": e.name, "description": e.description,
                 "priority": e.priority} for e in self._entries]

    def __len__(self) -> int:
        return len(self._entries)


# Singleton
_registry = FormatRegistry()

def get_registry() -> FormatRegistry:
    return _registry

def register(entry: FormatEntry) -> None:
    _registry.register(entry)
