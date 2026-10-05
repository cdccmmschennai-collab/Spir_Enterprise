"""
services/plant_classifier.py
----------------------------
Workbook-level Plant / Plant Description classification.

Flow (structure-driven — no fixed coordinates):
  1. Scan every visible sheet for project-level labels (PROJECT/PLANT, PLANT,
     MPP / PLANT CODE, cover-page TITLE). Labels can move between rows/columns.
  2. Read the label's value: text after the colon in the label cell itself,
     else the next non-empty, non-label cell to the right on the same row.
  3. Normalise the value and match it against the controlled PLANT_MASTER using
     the rule tiers below (highest-priority match is reported).
  4. Consistency-check every value in the workbook: one distinct plant ->
     RESOLVED; zero -> UNRESOLVED; more than one -> CONFLICT (never pick one).

Rule tiers (all rules read ONLY project-level label values):
  1. Explicit plant/MPP code — the whole value is exactly one of the nine codes.
  2. Exact controlled plant description as a whole phrase.
  3. Strong identifiers established from the sample SPIR investigation.
  4. Approved contextual aliases.

Hard exclusions (by construction — these sources are never read):
  COMPANY, vendor/focal-point blocks, REQUISITION/PO numbers, annexure
  SITE/Location columns, filenames, document properties. Plant codes are only
  accepted as a whole value, so "PO2400550" can never yield 2400. Generic
  words (MESAIEED, RAS LAFFAN, RLIC, DOHA, QATAR PETROLEUM) have no rule.

Stateless and concurrency-safe: module-level state is immutable.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

import structlog

log = structlog.stdlib.get_logger(__name__)

# ---------------------------------------------------------------------------
# Controlled Plant Master (the only values this module may output)
# ---------------------------------------------------------------------------

PLANT_MASTER: dict[str, str] = {
    "2600": "Ras Laffan Offshore Fields",
    "2300": "Dukhan Fields",
    "2500": "Refinery Mesaieed",
    "1200": "Qatar Petroleum - Doha",
    "2800": "Mesaieed Industrial City",
    "2900": "North Field Alpha",
    "2400": "NGL Mesaieed",
    "2700": "Ras Laffan Industrial City",
    "3000": "RL Cooling Water Systems",
}

STATUS_RESOLVED = "RESOLVED"
STATUS_UNRESOLVED = "UNRESOLVED"
STATUS_CONFLICT = "CONFLICT"

# ---------------------------------------------------------------------------
# Label detection
# ---------------------------------------------------------------------------

# Project-title labels. Anchored at the start of the cell and requiring a colon
# (or end of cell) so phrases like "RECOMMENDED BY PROJECT TEAM" never match.
_PROJECT_LABEL_RE = re.compile(
    r"^\s*(?:"
    r"PROJECT\s*/\s*PLANT"
    r"|PROJECT\s*(?:TITLE|NAME|DESCRIPTION)?"
    r"|PLANT(?:\s*(?:NAME|DESCRIPTION))?"
    r"|TITLE"
    r")\s*(?::|$)",
    re.IGNORECASE,
)

# Explicit plant-code labels (tier 1 source).
_CODE_LABEL_RE = re.compile(
    r"^\s*(?:"
    r"MPP(?:\s*CODE)?"
    r"|MAINTENANCE\s*PLANNING\s*PLANT"
    r"|PLANT\s*(?:CODE|NO\.?|NUMBER)"
    r")\s*(?::|$)",
    re.IGNORECASE,
)

# A neighbouring cell that is itself a label (ends with ':' or is a known
# header-block label) terminates the value search — e.g. the
# "MANUFACTURERS/SUPPLIERS FOCAL POINT ... NO:" cell beside an empty project.
_NEIGHBOUR_LABEL_RE = re.compile(
    r":\s*$"
    r"|^\s*(?:MANUFACTURERS?|SUPPLIERS?|COMPANY|CONTRACT|ENGINEERING\s+BY|"
    r"ISSUE\s+(?:LETTER|DATE)|SIGNATURE|SHEET\s+NO|REQUISITION|REMINDER|FIELD\s+\d)",
    re.IGNORECASE,
)

# Inside a multi-line label cell ("PROJECT/PLANT : X\nCONTRACT No. : Y"),
# the value stops where the next "LABEL :" line begins.
_NEXT_LABEL_LINE_RE = re.compile(r"\n\s*[A-Za-z][A-Za-z .&/()-]{1,40}:")

_MAX_VALUE_LOOKAHEAD_COLS = 8
_MAX_LABEL_CELL_LEN = 400

# ---------------------------------------------------------------------------
# Matching rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Rule:
    name: str
    plant: str
    tier: int
    confidence: str
    all_of: tuple[re.Pattern, ...]


def _p(pattern: str) -> re.Pattern:
    return re.compile(pattern)


def _phrase(text: str) -> re.Pattern:
    return re.compile(r"\b" + re.escape(text) + r"\b")


def _normalize(text: Any) -> str:
    """Uppercase, unify dashes, drop punctuation (keep '/' '&'), collapse spaces."""
    s = str(text).upper()
    s = re.sub(r"[‐-―−]", "-", s)
    s = re.sub(r"[^A-Z0-9/&\- ]+", " ", s)
    s = re.sub(r"\s+-\s+|\s+-|-\s+", " ", s)  # stand-alone dashes are separators
    return re.sub(r"\s+", " ", s).strip()


# Tier 2 — controlled descriptions, normalised the same way as the input.
_DESCRIPTION_RULES: tuple[_Rule, ...] = tuple(
    _Rule(f"exact_description:{code}", code, 2, "high", (_phrase(_normalize(desc)),))
    for code, desc in PLANT_MASTER.items()
)

# NGL with optional numbered unit: NGL, NGL1, NGL 2, NGL-3, NGL-1/2.
_NGL = r"\bNGL(?:\s*-?\s*\d+(?:/\d+)*)?\b"
_COOLING_SEAWATER = r"COOLING\s+SEA\s?WATER\s+SYSTEMS?\b"

# Tiers 3/4 — evidence-based identifiers (see plant investigation report).
_CONTEXT_RULES: tuple[_Rule, ...] = (
    _Rule("dukhan", "2300", 3, "high", (_p(r"\bDUKHAN\b"),)),
    _Rule("dpfu", "2300", 3, "high", (_p(r"\bDPFU\b"),)),
    _Rule("ngl+mesaieed", "2400", 3, "high", (_p(_NGL), _p(r"\bMESAIEED\b"))),
    _Rule("common_cooling_seawater_system", "3000", 3, "high",
          (_p(r"\bCOMMON\s+" + _COOLING_SEAWATER),)),
    _Rule("rlcsf", "3000", 3, "high", (_p(r"\bRLCSF\d*\b"),)),
    _Rule("cooling_seawater+rlic", "3000", 4, "medium",
          (_p(r"\b" + _COOLING_SEAWATER), _p(r"\bRLIC\b"))),
    _Rule("mic", "2800", 4, "medium", (_p(r"\bMIC\b"),)),
)

_TEXT_RULES: tuple[_Rule, ...] = _DESCRIPTION_RULES + _CONTEXT_RULES

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class PlantEvidence:
    """One project-level value found in the workbook and what it matched."""
    sheet: str
    cell: str
    label: str
    text: str
    normalized: str
    plants: list[str] = field(default_factory=list)
    rule: Optional[str] = None
    tier: Optional[int] = None
    confidence: Optional[str] = None


@dataclass
class PlantClassification:
    status: str
    plant: Optional[str] = None
    plant_description: Optional[str] = None
    rule: Optional[str] = None
    confidence: Optional[str] = None
    reason: str = ""
    evidence: list[PlantEvidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Value matching
# ---------------------------------------------------------------------------


def _match_code(value: Any) -> Optional[str]:
    """Return the plant code iff the WHOLE value is exactly one controlled code."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if float(value).is_integer():
            value = str(int(value))
        else:
            return None
    s = str(value).strip()
    return s if s in PLANT_MASTER else None


def _match_text(normalized: str) -> list[_Rule]:
    return [r for r in _TEXT_RULES if all(p.search(normalized) for p in r.all_of)]


def _classify_value(ev: PlantEvidence, raw_value: Any, code_label: bool) -> None:
    """Fill ev.plants / rule / tier / confidence for one label value."""
    code = _match_code(raw_value)
    if code is not None:
        ev.plants, ev.rule, ev.tier, ev.confidence = [code], "explicit_code", 1, "explicit"
        return
    if code_label:
        return  # code labels only ever accept an exact controlled code
    hits = _match_text(ev.normalized)
    if not hits:
        return
    best = min(hits, key=lambda r: r.tier)
    ev.plants = sorted({r.plant for r in hits})
    ev.rule, ev.tier, ev.confidence = best.name, best.tier, best.confidence


# ---------------------------------------------------------------------------
# Workbook scanning
# ---------------------------------------------------------------------------


def _iter_cells(ws) -> Iterable[tuple[int, int, Any]]:
    """Yield (row, col, value) for populated cells without materialising blanks."""
    cells = getattr(ws, "_cells", None)
    if isinstance(cells, dict):
        for (r, c), cell in cells.items():
            if cell.value is not None:
                yield r, c, cell.value
        return
    for row in ws.iter_rows():
        for cell in row:
            if getattr(cell, "value", None) is not None:
                yield cell.row, cell.column, cell.value


def _split_label(text: str, label_re: re.Pattern) -> Optional[tuple[str, str]]:
    """Return (label, inline_value) when text starts with a label, else None."""
    m = label_re.match(text)
    if not m:
        return None
    rest = text[m.end():]
    nxt = _NEXT_LABEL_LINE_RE.search(rest)
    if nxt:
        rest = rest[: nxt.start()]
    return m.group(0).strip().rstrip(":").strip(), rest.strip()


def _value_to_right(grid: dict[tuple[int, int], Any], row: int, col: int) -> tuple[Optional[str], Any]:
    for c in range(col + 1, col + 1 + _MAX_VALUE_LOOKAHEAD_COLS):
        v = grid.get((row, c))
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        if isinstance(v, str) and _NEIGHBOUR_LABEL_RE.search(v.strip()):
            return None, None
        return _coord(row, c), v
    return None, None


def _coord(row: int, col: int) -> str:
    letters = ""
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row}"


def collect_evidence(wb) -> list[PlantEvidence]:
    """Find every project-level label value in the visible sheets of wb."""
    evidence: list[PlantEvidence] = []
    for ws in wb.worksheets:
        if getattr(ws, "sheet_state", "visible") != "visible":
            continue
        grid: dict[tuple[int, int], Any] = {}
        labels: list[tuple[int, int, str, str, bool]] = []
        for r, c, v in _iter_cells(ws):
            grid[(r, c)] = v
            if not isinstance(v, str) or len(v) > _MAX_LABEL_CELL_LEN:
                continue
            for label_re, is_code in ((_CODE_LABEL_RE, True), (_PROJECT_LABEL_RE, False)):
                split = _split_label(v, label_re)
                if split:
                    labels.append((r, c, split[0], split[1], is_code))
                    break
        for r, c, label, inline, is_code in labels:
            if inline:
                cell, raw = _coord(r, c), inline
            else:
                cell, raw = _value_to_right(grid, r, c)
            text = "" if raw is None else re.sub(r"\s+", " ", str(raw)).strip()
            if not text:
                # Keep empty labels in the audit trail (they explain UNRESOLVED).
                evidence.append(PlantEvidence(sheet=ws.title, cell=_coord(r, c),
                                              label=label.upper(), text="", normalized=""))
                continue
            ev = PlantEvidence(sheet=ws.title, cell=cell, label=label.upper(),
                               text=text, normalized=_normalize(text))
            _classify_value(ev, raw, is_code)
            evidence.append(ev)
    return evidence


def classify_evidence(evidence: list[PlantEvidence]) -> PlantClassification:
    """Consistency-check all evidence and produce the workbook classification."""
    matched = [e for e in evidence if e.plants]
    if not matched:
        if not evidence:
            reason = "no project-level label found"
        elif not any(e.text for e in evidence):
            reason = "project-level label(s) found but empty"
        else:
            reason = "project-level text does not identify a controlled plant"
        return PlantClassification(STATUS_UNRESOLVED, reason=reason, evidence=evidence)

    plants = sorted({p for e in matched for p in e.plants})
    if len(plants) > 1:
        return PlantClassification(
            STATUS_CONFLICT,
            reason=f"project-level values point to different plants: {', '.join(plants)}",
            evidence=evidence,
        )

    code = plants[0]
    best = min(matched, key=lambda e: e.tier or 99)
    return PlantClassification(
        STATUS_RESOLVED,
        plant=code,
        plant_description=PLANT_MASTER[code],
        rule=best.rule,
        confidence=best.confidence,
        reason=f"{best.sheet}!{best.cell}: {best.text[:120]}",
        evidence=evidence,
    )


def classify_plant(wb) -> PlantClassification:
    """Classify the workbook's plant. Never raises — failures are UNRESOLVED."""
    try:
        result = classify_evidence(collect_evidence(wb))
    except Exception as exc:  # classification must never break extraction
        log.warning("plant.classify_failed", error=str(exc))
        return PlantClassification(STATUS_UNRESOLVED, reason=f"classifier error: {exc}")
    log.info(
        "plant.classified",
        status=result.status,
        plant=result.plant,
        rule=result.rule,
        reason=result.reason,
        values=len(result.evidence),
    )
    return result
