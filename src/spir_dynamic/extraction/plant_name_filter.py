"""
extraction/plant_name_filter.py
-------------------------------
Strips plant / location names that vendors concatenate onto equipment
make / model values, e.g.

    "AB' GLM RIO PANEL-KHATIYA"  ->  "AB' GLM RIO PANEL"

The plant is carried by the user-selected PLANNING PLANT columns
(services/planning_plant); it must never leak into EQPT MAKE / EQPT MODEL.

Matching is driven by the plant-name master below, not by position:
  - a plant name joined to the END of a value by a delimiter
    (- / _ , : | parentheses or whitespace) is removed
  - a plant name joined to the START by a hard delimiter (not whitespace) is removed
  - a value that is nothing but a plant name is cleared
Vendors abbreviate / misspell plants ("KHATIYA" for "Khatiyah"), so names of
6+ characters also match at edit distance 1. Generic qualifiers
(MAIN / NORTH / SOUTH / FIELD(S) / ISLAND) are dropped to derive the short
form a vendor typically writes ("Khatiyah Main" -> "KHATIYAH").
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Iterable

# Plant / location master (QatarEnergy plant list).
PLANT_NAMES: tuple[str, ...] = (
    "ARBC", 'Arab "C"', 'Arab "D"', "Berth 6 Rfnry Export Import",
    "PWI Cluster 1 Dukhan", "PWI Cluster 2 Dukhan", "PWI Cluster 3 Dukhan",
    "COMMUNITY SERVICES", "Dukhan Cathodic Prtctn Satns", "Dukhan Water Storage & Dist",
    "Dukhan - Field Support Logisti", "Dukhan Power Plant", "Power Dist Dukhan",
    "Dukhan Sewage Plant", "Dukhan Support Services", "DIYAB",
    "Fahahil Main", "Fahahil North", "Fahahil South", "Fahahil North Gas Lift Comp",
    "Fahahil North Field", "Fahahil Stripping Plant", "Gas Flowlines - Dukhan",
    "GENERAL SERVICES", "Halul  Island", "Jaleha Main",
    "Khatiyah Main", "Khatiyah North", "Khatiyah South",
    "Khuff Gas Station A", "Khuff Gas Station B", "Khuff Gas Station C",
    "Khuff Gas Station D", "Khuff Gas Station E", "Khuff Gas Station G",
    "Khuff Gas Station H", "Khuff Gas Stations L", "Dukhan Line Break Valve Statns",
    "MIC WASTE MANAGEMENT", "MIC INFRASTRUCTURE", "Mesaieed Industrial City",
    "Mesaieed Port", "Marine Operations", "North Field Alpha",
    "Mesaieed Common Facilities", "Mesaieed Common Utilities",
    "NGL Gas Distribution Stations", "NGL1 Mesaieed", "NGL2 Mesaieed",
    "NGL3 Mesaieed", "NGL4 Mesaieed", "NGL  Gas Sweetening  Facility",
    "NGL  Storage & Loading", "Oil Tank Farm & Terminal", "Oil Flowlines - Dukhan",
    "OPERATION ENGINEERING SUPPORT SYSTEM", "Offsite Tank Frm & Gnrl Bldngs",
    "OSER Cooling Water Facility", "OSER Dukhan", "OSER Doha", "OSER Halul",
    "OSER Meassaieed Indus City", "OSER Refinery", "OSER Ras Laffan Indus City",
    "Khuff Point N", "Khuff Point U", "PS2 Maydan Mahzam", "PS3 Bul Hanine",
    "Powered Water Inj Station 1", "Powered Water Inj Station 2",
    "Powered Water Inj Station 3", "Powered Water Inj Station 4",
    "Powered Water Inj Station 5", "Powered Water Inj Station 6",
    "Powered Water Inj Station 7", "Powered Water Inj Station 8",
    "Powered Water Inj Station 9", "Power Water Injecn Ring Main",
    "AQP- Caltex Joint Venture Co", "CHEMICAL STORES",
    "Ras Laffan Accom Camp 1", "Ras Laffan Accom Camp 2", "Condensate Refinery",
    "Ras Laffan Common Seawater Fac", "Refinery 1", "Refinery 2",
    "Refinery Fluid Catalytic Conve", "Refinery Facilities",
    "Refinery Linear Alkyl Benzene", "Ras Laffan Infrastructure",
    "RAS LAFFAN INFRASTRUCTURE WEST", "LPG Bottling Plants No.1 & 2",
    "Ras Laffan Port", "Refinery Utilities", "RAS LAFFAN WASTEWATER TREATMENT PLANTS",
    "Umm Bab Oil Pumping Station", "Offshore Vessels/WorkBoats",
    "Qatar Fuel Co WOQOD Doha Depot", "Marine Dept Workshops",
    "Ras Laffan Offshore Fields", "Dukhan Fields", "Refinery Mesaieed",
    "Qatar Petroleum - Doha", "NGL Mesaieed", "Ras Laffan Industrial City",
    "RL Cooling Water Systems",
)

# Trailing qualifiers dropped to derive a plant's short form.
_GENERIC_QUALIFIERS = frozenset({"MAIN", "NORTH", "SOUTH", "FIELD", "FIELDS", "ISLAND"})

_MIN_KEY_LEN = 4      # shorter derived forms are too ambiguous to strip
_FUZZY_MIN_LEN = 6    # edit distance 1 allowed only for names this long

# Separators a vendor uses to join a plant onto a value.
_HARD_SEPS = "-–—/_,:|("
_SEP_RE = re.compile(r"[\s\-–—/_,:|()]")
_TRIM_CHARS = " \t\r\n-–—/_,:|()"


def _norm(text: str) -> str:
    """Uppercase, punctuation -> space, collapse whitespace."""
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", str(text).upper()).split())


@lru_cache(maxsize=1)
def _plant_keys() -> frozenset[str]:
    keys: set[str] = set()
    for name in PLANT_NAMES:
        tokens = _norm(name).split()
        while tokens:
            key = " ".join(tokens)
            if len(key.replace(" ", "")) >= _MIN_KEY_LEN and not key.replace(" ", "").isdigit():
                keys.add(key)
            if len(tokens) > 1 and tokens[-1] in _GENERIC_QUALIFIERS:
                tokens = tokens[:-1]
            else:
                break
    return frozenset(keys)


def _within_one_edit(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = j = 0
    edited = False
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        if edited:
            return False
        edited = True
        if len(a) == len(b):
            i += 1
        j += 1
    return True


def is_plant_name(text: str) -> bool:
    """True when `text` is a plant name from the master (or its short form)."""
    seg = _norm(text)
    if not seg:
        return False
    keys = _plant_keys()
    if seg in keys:
        return True
    compact = seg.replace(" ", "")
    if len(compact) < _FUZZY_MIN_LEN - 1:
        return False
    for key in keys:
        key_compact = key.replace(" ", "")
        if len(key_compact) >= _FUZZY_MIN_LEN and _within_one_edit(compact, key_compact):
            return True
    return False


def _strip_once(value: str) -> str | None:
    """Remove one concatenated plant name; return None when nothing matched."""
    # Suffix: try the longest candidate first so multi-word plants win.
    for m in _SEP_RE.finditer(value):
        core = value[:m.start()].rstrip(_TRIM_CHARS)
        seg = value[m.end():].strip(_TRIM_CHARS)
        if _norm(core) and is_plant_name(seg):
            return core
    # Prefix: hard delimiter only — whitespace is too loose at the start.
    for m in reversed(list(_SEP_RE.finditer(value))):
        if m.group() not in _HARD_SEPS:
            continue
        seg = value[:m.start()].strip(_TRIM_CHARS)
        rest = value[m.end():].strip(_TRIM_CHARS)
        if _norm(rest) and is_plant_name(seg):
            return rest
    return None


def strip_plant_names(value: Any) -> Any:
    """
    Return `value` with concatenated plant names removed.

    Non-strings pass through unchanged. A value that is only a plant name
    becomes None (it carries no make / model information).
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    if is_plant_name(text):
        return None
    changed = False
    while True:
        stripped = _strip_once(text)
        if stripped is None:
            break
        text, changed = stripped, True
    return text if changed else value


def strip_plant_names_in_columns(rows: Iterable[list], cols: Iterable[int | None]) -> None:
    """Apply strip_plant_names in place to the given column indexes of every row."""
    idxs = [c for c in cols if c is not None]
    for row in rows:
        for c in idxs:
            if c < len(row):
                row[c] = strip_plant_names(row[c])
