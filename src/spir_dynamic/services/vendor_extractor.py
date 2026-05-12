"""
services/vendor_extractor.py
-----------------------------
Extracts vendor contact details from the MANUFACTURERS/SUPPLIERS FOCAL POINT
cell found at the bottom of SPIR main sheets.

Stateless and concurrency-safe: no module-level mutable state, no shared objects.
Safe under Celery parallel workers.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from sqlalchemy import text

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Compiled constants (immutable after module load — safe for concurrent use)
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Words that strongly indicate a company name (vs. a person name or address line).
# Used to decide whether the focal-point cell's first line is a company name.
_COMPANY_INDICATORS = re.compile(
    r"\b(?:ltd|llc|l\.l\.c|inc|corp|group|systems?|services?|engineering|"
    r"industries?|industrial|international|company|trading|supply|supplies|"
    r"solutions?|technolog(?:y|ies)|automation|controls?|petroleum|oil|gas|"
    r"energy|power|valve|pump|meter|safety|manufacturing|construction|"
    r"consulting|contractors?|co\b|&)\b",
    re.IGNORECASE,
)

# Focal point cell detection keywords, ordered by specificity
_FOCAL_KEYWORDS: tuple[str, ...] = (
    "manufacturers/suppliers focal point",
    "manufacturers suppliers focal point",
    "manufacturer/supplier focal point",
    "manufacturers/suppliers focal point including",
    "focal point including",
    "focal point including e-mail",
    "email/ tel/fax",
    "e-mail/ tel/fax",
    "tel/fax",
    "focal point",
    "suppliers focal",
) 

# Lines that are clearly part of the label block (skip when finding vendor name).
# Use word boundaries for short tokens (tel, fax) to avoid false matches inside
# city names like "Telford" or product names containing "fax".
_LABEL_SKIP_RE = re.compile(
    r"manufacturers?|suppliers?|focal\s+point|e-?mail|\btel\b|\bfax\b",
    re.IGNORECASE,
)

# Known country names (uppercase, dot-stripped for normalization)
_KNOWN_COUNTRIES: frozenset[str] = frozenset({
    "INDIA", "CHINA", "USA", "UNITED STATES", "UNITED STATES OF AMERICA",
    "UK", "UNITED KINGDOM", "ENGLAND", "SCOTLAND", "WALES",
    "GERMANY", "FRANCE", "JAPAN", "SOUTH KOREA", "KOREA", "ITALY",
    "SINGAPORE", "UAE", "UNITED ARAB EMIRATES", "QATAR", "SAUDI ARABIA",
    "AUSTRALIA", "CANADA", "NETHERLANDS", "SWEDEN", "SWITZERLAND",
    "BRAZIL", "MEXICO", "RUSSIA", "SPAIN", "TURKEY", "INDONESIA",
    "THAILAND", "MALAYSIA", "VIETNAM", "TAIWAN", "HONG KONG",
    "ISRAEL", "IRAN", "IRAQ", "PAKISTAN", "BANGLADESH", "SRI LANKA",
    "MYANMAR", "PHILIPPINES", "NEW ZEALAND", "SOUTH AFRICA", "EGYPT",
    "NIGERIA", "KENYA", "ETHIOPIA", "ARGENTINA", "COLOMBIA", "CHILE",
    "PERU", "VENEZUELA", "CZECH REPUBLIC", "POLAND", "HUNGARY",
    "ROMANIA", "BULGARIA", "AUSTRIA", "BELGIUM", "DENMARK", "FINLAND",
    "NORWAY", "PORTUGAL", "GREECE", "UKRAINE", "IRELAND",
    "OMAN", "KUWAIT", "BAHRAIN", "JORDAN", "LEBANON",
    "ALGERIA", "MOROCCO", "TUNISIA", "GHANA", "TANZANIA",
    "CAMBODIA", "NEPAL",
})

_CITY_TO_COUNTRY: dict[str, str] = {
    "NAVI MUMBAI": "INDIA", "MUMBAI": "INDIA", "CHENNAI": "INDIA",
    "DELHI": "INDIA", "NEW DELHI": "INDIA", "BANGALORE": "INDIA",
    "BENGALURU": "INDIA", "PUNE": "INDIA", "HYDERABAD": "INDIA",
    "AHMEDABAD": "INDIA", "VADODARA": "INDIA", "GUJARAT": "INDIA",
    "KOLKATA": "INDIA", "SURAT": "INDIA", "NOIDA": "INDIA",
    "YANCHENG": "CHINA", "SHANGHAI": "CHINA", "BEIJING": "CHINA",
    "GUANGZHOU": "CHINA", "SHENZHEN": "CHINA", "TIANJIN": "CHINA",
    "WUHAN": "CHINA", "CHENGDU": "CHINA", "NANJING": "CHINA",
    "MILAN": "ITALY", "MILANO": "ITALY", "ROME": "ITALY",
    "PARIS": "FRANCE", "LYON": "FRANCE",
    "BERLIN": "GERMANY", "HAMBURG": "GERMANY", "MUNICH": "GERMANY",
    "TOKYO": "JAPAN", "OSAKA": "JAPAN",
    "SEOUL": "SOUTH KOREA", "BUSAN": "SOUTH KOREA",
    "DUBAI": "UAE", "ABU DHABI": "UAE", "SHARJAH": "UAE",
    "RIYADH": "SAUDI ARABIA", "JEDDAH": "SAUDI ARABIA",
    "AMSTERDAM": "NETHERLANDS", "ROTTERDAM": "NETHERLANDS",
    "STOCKHOLM": "SWEDEN", "ZURICH": "SWITZERLAND", "GENEVA": "SWITZERLAND",
    "BRUSSELS": "BELGIUM", "COPENHAGEN": "DENMARK",
    "SYDNEY": "AUSTRALIA", "MELBOURNE": "AUSTRALIA",
    "SAO PAULO": "BRAZIL", "RIO DE JANEIRO": "BRAZIL",
    "MOSCOW": "RUSSIA", "MADRID": "SPAIN", "BARCELONA": "SPAIN",
    "ISTANBUL": "TURKEY", "ANKARA": "TURKEY",
}

_PHONE_PREFIX_TO_COUNTRY: dict[str, str] = {
    "+974": "QATAR", "+971": "UAE", "+966": "SAUDI ARABIA",
    "+968": "OMAN", "+965": "KUWAIT", "+973": "BAHRAIN",
    "+91": "INDIA", "+86": "CHINA", "+81": "JAPAN",
    "+82": "SOUTH KOREA", "+65": "SINGAPORE", "+44": "UK",
    "+49": "GERMANY", "+33": "FRANCE", "+39": "ITALY",
    "+31": "NETHERLANDS", "+46": "SWEDEN", "+41": "SWITZERLAND",
    "+61": "AUSTRALIA", "+55": "BRAZIL", "+7": "RUSSIA",
    "+34": "SPAIN", "+90": "TURKEY", "+48": "POLAND",
    "+32": "BELGIUM", "+45": "DENMARK", "+47": "NORWAY",
    "+351": "PORTUGAL", "+30": "GREECE", "+353": "IRELAND",
    "+98": "IRAN",
}

# Labeled phone block regex — captures label + the immediate phone number.
# nums stops at characters not found in phone numbers (comma, letter, slash)
# so that "Tel: +974 1234, Fax: +974 5678" produces two separate matches.
_LABELED_PHONE_RE = re.compile(
    r"(?P<label>(?:fax|telephone|tel|mobile|mob|cell|phone|ph)\b[\w\s\.]*?)"
    r"\s*[:：]\s*"
    r"(?P<nums>[\+\(\d][\+\d\s\-\(\)\.]{5,35})",
    re.IGNORECASE,
)

# Bare phone patterns:
#   +XX ...   international with + prefix
#   00XX ...  international with 00 prefix (e.g. 0044 (0) 1952 290 321)
#   0XX-XXX   local with area/STD code
_BARE_PHONE_RE = re.compile(
    r"(?<![/@\w])"
    r"(\+[\d][\d\s\-\(\)\.]{6,25}"
    r"|00\d{1,3}[\s\-\(][\d\s\-\(\)\.]{5,25}"
    r"|\b0\d{2,4}[\s\-]\d{3,})"
    r"(?![/@\w])"
)


# Vendor contact block: cell starts with SUPPLIER:/MANUFACTURER:/VENDOR: and
# contains contact info. Used as secondary scan when no focal point label found.
_VENDOR_BLOCK_RE = re.compile(
    r"^(?:supplier|manufacturer|vendor)\s*[:\/]",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_focal_point_cell(ws) -> Optional[str]:
    """
    Scan a worksheet for the MANUFACTURERS/SUPPLIERS FOCAL POINT cell.

    Scans the bottom 30 rows first, then falls back to a full-sheet scan.
    If no focal point keyword is found, tries a secondary scan for vendor
    contact blocks (cells starting with SUPPLIER:/MANUFACTURER:/VENDOR: that
    also contain an email or phone number).

    Returns the full cell text (may be multi-line via embedded \\n) or None.
    """
    if getattr(ws, "sheet_state", "visible") in ("hidden", "veryHidden"):
        log.debug("vendor_extractor: skipping hidden sheet '%s'", getattr(ws, "title", "?"))
        return None

    max_row: int = ws.max_row or 0
    max_col: int = ws.max_column or 0

    if max_row == 0 or max_col == 0:
        log.debug("vendor_extractor: empty sheet, skipping focal point scan")
        return None

    bottom_start = max(1, max_row - 30)
    result = _scan_rows(ws, bottom_start, max_row, max_col)
    if result:
        return result

    if bottom_start > 1:
        result = _scan_rows(ws, 1, bottom_start - 1, max_col)

    if not result:
        result = _find_vendor_contact_block(ws, max_row, max_col)

    if not result:
        result = _find_contact_block(ws, max_row, max_col)

    if not result:
        log.debug("vendor_extractor: focal point cell not found in sheet")
    return result


def extract_vendor_details(text: str, supplier_name: str = "") -> dict:
    """
    Parse vendor contact details from a focal point cell's text content.

    Args:
        text:          Full text of the focal point cell (may contain newlines).
        supplier_name: Supplier name from the SPIR header (top-right field).
                       Used as primary vendor name source.

    Returns a dict with keys:
        vendor_name, email1, email2, contact, country
    All values are strings; empty string when not found.
    """
    result: dict[str, str] = {
        "vendor_name": "",
        "email1": "",
        "email2": "",
        "contact": "",
        "country": "",
    }

    # Vendor name — four-level priority:
    # 1. Explicit "Vendor:" / "Company:" label in the focal point cell
    # 2. First line that looks like a company name (contains Ltd, Services, &…)
    # 3. Header-level supplier name from SPIR header metadata
    # 4. Whatever first line the cell gives (last resort, no fallback available)
    explicit_name    = _extract_explicit_vendor_name(text) if text else ""
    company_from_cell = _extract_company_name(text) if text else ""
    header_supplier   = supplier_name.strip() if supplier_name else ""

    if explicit_name:
        result["vendor_name"] = explicit_name
    elif company_from_cell and _COMPANY_INDICATORS.search(company_from_cell):
        result["vendor_name"] = company_from_cell
    else:
        result["vendor_name"] = header_supplier or company_from_cell

    if not text:
        return result

    try:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = [ln.strip() for ln in normalized.split("\n") if ln.strip()]

        result["email1"], result["email2"] = _extract_emails(normalized)
        result["contact"] = _extract_contact_numbers(normalized)
        country_lines = lines + ([result["contact"]] if result.get("contact") else [])
        result["country"] = _extract_country(country_lines)
    except Exception as exc:  # noqa: BLE001
        log.debug("vendor_extractor: parsing failed: %s", exc)

    return result


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _find_contact_block(ws, max_row: int, max_col: int) -> Optional[str]:
    """
    Tertiary scan: find any multi-line cell containing both email and phone.
    Catches vendor contact blocks that have no recognizable keyword prefix.
    Only scans bottom 50 rows to avoid matching data cells in the body.
    """
    for r in range(max(1, max_row - 50), max_row + 1):
        for c in range(1, max_col + 1):
            v = ws.cell(r, c).value
            if not v:
                continue
            s = str(v).strip()
            if not s or "\n" not in s:
                continue
            if _EMAIL_RE.search(s) and _BARE_PHONE_RE.search(s):
                return s
    return None


def _find_vendor_contact_block(ws, max_row: int, max_col: int) -> Optional[str]:
    """
    Secondary scan for vendor contact blocks with no focal point label.

    Looks for cells that start with SUPPLIER:/MANUFACTURER:/VENDOR: AND contain
    at least one email address or phone number. Scans the bottom 50 rows to
    avoid picking up unrelated header cells at the top of the sheet.
    """
    for r in range(max(1, max_row - 50), max_row + 1):
        for c in range(1, max_col + 1):
            v = ws.cell(r, c).value
            if not v:
                continue
            s = str(v).strip()
            if not s:
                continue
            if _VENDOR_BLOCK_RE.match(s) and (
                _EMAIL_RE.search(s) or _BARE_PHONE_RE.search(s)
            ):
                return s
    return None


def _scan_rows(ws, start: int, end: int, max_col: int) -> Optional[str]:
    """Scan rows and return focal point DATA (not label)."""

    for r in range(start, end + 1):
        for c in range(1, max_col + 1):
            val = ws.cell(r, c).value
            if val is None:
                continue

            text = str(val).strip()
            if not text:
                continue

            text_lower = text.replace("\n", " ").lower()

            for kw in _FOCAL_KEYWORDS:
                if kw in text_lower:
                    # FOUND LABEL → now search for DATA nearby
                    # Strategies 1–4 only return immediately when useful data
                    # (email or phone) is found; otherwise fall through to Strategy 5.
                    candidate: Optional[str] = None

                    # 1. Right cell
                    if c + 1 <= max_col:
                        right = ws.cell(r, c + 1).value
                        if right:
                            s = str(right).strip()
                            if s and (_EMAIL_RE.search(s) or _BARE_PHONE_RE.search(s)):
                                return s
                            if s:
                                candidate = s

                    # 2. Below cell
                    if r + 1 <= ws.max_row:
                        below = ws.cell(r + 1, c).value
                        if below:
                            s = str(below).strip()
                            if s and (_EMAIL_RE.search(s) or _BARE_PHONE_RE.search(s)):
                                return s
                            if s and not candidate:
                                candidate = s

                    # 3. Diagonal (very common case)
                    if r + 1 <= ws.max_row and c + 1 <= max_col:
                        diag = ws.cell(r + 1, c + 1).value
                        if diag:
                            s = str(diag).strip()
                            if s and (_EMAIL_RE.search(s) or _BARE_PHONE_RE.search(s)):
                                return s
                            if s and not candidate:
                                candidate = s

                    # 4. Multi-line same-column block
                    collected = []
                    for i in range(1, 5):
                        if r + i <= ws.max_row:
                            v = ws.cell(r + i, c).value
                            if v:
                                collected.append(str(v).strip())
                    if collected:
                        block = "\n".join(collected)
                        if _EMAIL_RE.search(block) or _BARE_PHONE_RE.search(block):
                            return block

                    # 5. Structured table — scan multiple rows × multiple columns
                    # Handles cases where labels are in one column and values are
                    # spread across adjacent columns (e.g. Sulzer-style SPIR files).
                    lines: list[str] = []
                    empty_streak = 0
                    for i in range(1, 10):
                        if r + i > ws.max_row:
                            break
                        row_vals: list[str] = []
                        for dc in range(0, 9):
                            if c + dc > max_col:
                                break
                            v = ws.cell(r + i, c + dc).value
                            if v is not None:
                                sv = str(v).strip()
                                if sv:
                                    row_vals.append(sv)
                        if not row_vals:
                            empty_streak += 1
                            if empty_streak >= 2:
                                break
                            continue
                        empty_streak = 0
                        # Pair label cells (ending with ":") with the next value cell
                        paired: list[str] = []
                        skip = False
                        for j, val in enumerate(row_vals):
                            if skip:
                                skip = False
                                continue
                            if val.rstrip().endswith(":") and j + 1 < len(row_vals):
                                paired.append(f"{val} {row_vals[j + 1]}")
                                skip = True
                            else:
                                paired.append(val)
                        lines.extend(paired)

                    if lines:
                        wide = "\n".join(lines)
                        if _EMAIL_RE.search(wide) or _BARE_PHONE_RE.search(wide):
                            return wide
                        if len(lines) > 2:
                            return wide

                    # Last resort: return whatever candidate we found
                    if candidate:
                        return candidate

                    break  # stop searching keywords for this cell

    return None


def _extract_explicit_vendor_name(text: str) -> str:
    """
    Scan for an explicit 'Vendor:' or 'Company:' label in the focal point cell.
    Returns the value after the label (trimmed), or "" if not found.

    Handles both newline-separated and space-padded cell layouts.
    Matches at the start of any line (re.MULTILINE) to avoid grabbing embedded
    occurrences inside longer sentences.
    """
    pattern = re.compile(
        r"^(?:vendor|company|firm)\s*[:\s]\s*([^\n]{3,150})",
        re.IGNORECASE | re.MULTILINE,
    )
    m = pattern.search(text)
    if m:
        return m.group(1).strip().rstrip(",;")
    return ""


def _extract_company_name(text: str) -> str:
    """
    Extract the first meaningful company name line from focal point cell text.

    Skips the label line, phone lines, email lines, and pure address/zip lines.
    Returns the first remaining line that looks like a company name, or "".
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for line in normalized.split("\n"):
        line = line.strip()
        if not line:
            continue
        # Skip the focal point label itself
        if _LABEL_SKIP_RE.search(line) and len(line) < 80:
            continue
        # Skip lines that are only digits / zip codes / dashes
        if re.match(r"^[\d\s\-,\.]+$", line):
            continue
        # Skip email lines
        if _EMAIL_RE.search(line):
            continue
        # Skip phone/address lines starting with + or digit patterns
        if re.match(r"^[\+\d\(]", line) and re.search(r"\d{5,}", line.replace(" ", "")):
            continue
        # Must have at least 3 alphabetic characters to qualify as a name
        if len(re.findall(r"[A-Za-z]", line)) >= 3:
            return line.strip()
    return ""


def _extract_emails(text: str) -> tuple[str, str]:
    """
    Extract emails from text.

    Returns (email1, email2):
      - 0 emails → ("", "")
      - 1 email  → (email, "")
      - 2+ emails → (first, remaining joined by "," with no spaces)
    """
    found = _EMAIL_RE.findall(text)
    seen: set[str] = set()
    unique: list[str] = []
    for e in found:
        key = e.lower()
        if key not in seen:
            seen.add(key)
            unique.append(e)

    if not unique:
        return "", ""
    if len(unique) == 1:
        return unique[0], ""
    return unique[0], ",".join(unique[1:])


def _extract_contact_numbers(text: str) -> str:
    """
    Extract and format phone/fax contact numbers.

    Output format: prefix:num[,num],prefix:num
    """

    def _normalize_number(num: str) -> str:
        num = num.strip()

        # collapse multiple spaces
        num = re.sub(r"\s+", " ", num)

        # remove space after +
        num = re.sub(r"\+\s+", "+", num)

        return num

    def _digit_key(s: str) -> str:
        """Strip all non-digit chars for deduplication comparisons."""
        return re.sub(r"\D", "", s)

    clean = _EMAIL_RE.sub(" ", text)
    segments: list[str] = []

    # 🔹 Labeled blocks — track captured digit keys to avoid re-extracting them
    labeled_keys: set[str] = set()

    for m in _LABELED_PHONE_RE.finditer(clean):
        label = m.group("label").strip()
        num_str = m.group("nums").strip().rstrip(",;/ ").strip()
        prefix = _label_to_prefix(label)

        if not re.search(r"\d{5,}", num_str.replace(" ", "").replace("-", "")):
            continue

        n = _normalize_number(num_str)
        for sub in _BARE_PHONE_RE.findall(n):
            labeled_keys.add(_digit_key(sub))
        labeled_keys.add(_digit_key(n))
        segments.append(f"{prefix}:{n}")

    # 🔹 Bare (unlabeled) numbers — also run when labeled blocks exist so that
    # numbers with unrecognized labels (e.g. "Line :") are still captured.
    bare_matches = _BARE_PHONE_RE.findall(clean)

    bare_nums = [
        _normalize_number(b)
        for b in bare_matches
        if re.search(r"\d{5,}", b.replace(" ", "").replace("-", ""))
        and _digit_key(b) not in labeled_keys  # skip already-captured numbers
    ]

    if bare_nums:
        classified: list[tuple[str, str]] = [
            (_classify_unlabeled(n), n) for n in bare_nums
        ]

        # Merge same-prefix groups
        i = 0
        while i < len(classified):
            prefix, num = classified[i]
            group = [num]
            j = i + 1
            while j < len(classified) and classified[j][0] == prefix:
                group.append(classified[j][1])
                j += 1
            segments.append(f"{prefix}:{','.join(group)}" if prefix else ",".join(group))
            i = j

    return ",".join(segments)



def _label_to_prefix(label: str) -> str:
    """Map a phone label string to its normalized prefix."""
    lower = label.lower()
    if "fax" in lower:
        return "fax"
    if "mobile" in lower or "mob" in lower or "cell" in lower:
        return "mobile"
    if "tel" in lower:  # catches both "tel" and "telephone"
        return "tel"
    return "phone"


def _classify_unlabeled(num: str) -> str:
    """
    Classify an unlabeled phone number as 'tel' or 'phone'.

    Logic:
    - Has area/country code pattern → 'tel'
    - Otherwise → 'phone'
    """
    # +CC AA XXXXXXX (international with + and area code)
    if re.match(r"^\+\d{1,3}[\s\-]\d{2,4}[\s\-]\d", num):
        return "tel"
    # 00CC ... (international with 00 prefix, e.g. 0044 (0) 1952 290 321)
    if re.match(r"^00\d{1,3}[\s\-\(]", num):
        return "tel"
    # Local STD code: 0XX-XXXX or 0XXX-XXXX
    if re.match(r"^0\d{2,4}[\s\-]", num):
        return "tel"
    return "phone"


def _extract_country(lines: list[str]) -> str:
    """
    Identify the country name from address lines.

    Scans lines in reverse (country is typically the last address token).
    Normalizes each line: uppercase, dots removed, stripped.
    """
    for line in reversed(lines):
        normalized = line.upper().replace(".", "").strip()

        # Strategy 1: whole normalized line is a known country
        if normalized in _KNOWN_COUNTRIES:
            return normalized

        # Strategy 2: comma-split — last or any part is a country
        parts = [p.strip() for p in normalized.split(",")]
        for part in reversed(parts):
            if part in _KNOWN_COUNTRIES:
                return part

        # Strategy 3: word-boundary match within the normalized line
        for country in _KNOWN_COUNTRIES:
            pattern = r"\b" + re.escape(country) + r"\b"
            if re.search(pattern, normalized):
                return country

    # Strategy 4: city / region name → country (longest match first)
    full_text_upper = " ".join(lines).upper()
    for city, ctry in sorted(_CITY_TO_COUNTRY.items(), key=lambda x: -len(x[0])):
        if city in full_text_upper:
            return ctry

    # Strategy 5: phone country-code prefix → country (longest prefix first)
    for prefix, ctry in sorted(_PHONE_PREFIX_TO_COUNTRY.items(), key=lambda x: -len(x[0])):
        if re.search(r'(?<!\d)' + re.escape(prefix), full_text_upper):
            return ctry

    return ""
