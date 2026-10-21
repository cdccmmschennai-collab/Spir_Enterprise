"""
Saipem pipeline tail: extract → build the Saipem workbook → store → response.

Called from app/pipeline.run_pipeline once detect_saipem_sheets() has claimed
the workbook. The generic shared steps are deliberately not applied:
Planning Plant columns, QAR currency conversion, POSITION / OMN-SPF numbers,
duplicate analysis and the ERROR column.

The response carries the same keys as the generic pipeline so every caller
(sync route, Celery task, batch router, audit / history) keeps working. The
Saipem files use no Planning Plant: requests in Saipem mode send none
(plant is None). A plant that does arrive (auto-detected Saipem file from a
client without a mode) is only echoed in the response metadata, never
written as an output column.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any, Optional

import structlog

from spir_dynamic.extraction.saipem.errors import NO_SAIPEM_ROWS_MESSAGE, SaipemFormError
from spir_dynamic.extraction.saipem.excel_builder import build_saipem_xlsx
from spir_dynamic.extraction.saipem.extractor import extract_saipem
from spir_dynamic.extraction.saipem.profile import flat_label
from spir_dynamic.monitoring.metrics import EXTRACTION_ROWS
from spir_dynamic.services.extraction_mode import MODE_SAIPEM
from spir_dynamic.services.planning_plant import PlanningPlant
from spir_dynamic.services.storage import get_storage

log = structlog.stdlib.get_logger(__name__)

FORMAT_NAME = "SAIPEM"


def run_saipem_pipeline(
    wb,
    sheet_names: list[str],
    original_filename: str,
    planning_plant: Optional[PlanningPlant],
) -> dict[str, Any]:
    result = extract_saipem(wb, sheet_names)
    for w in result.warnings:
        log.warning("saipem.warning", filename=original_filename, detail=w)
    if not result.rows:
        # A recognised form that yields no tag/spare rows is not a valid
        # extraction — fail (reasons are in the warnings logged above), never
        # export it empty.
        raise SaipemFormError(NO_SAIPEM_ROWS_MESSAGE)

    xlsx_bytes = build_saipem_xlsx(result.rows, result.columns, result.band_labels, result.spir_no)

    file_id = str(uuid.uuid4())
    safe_stem = re.sub(r'[\r\n\t/\\:*?"<>|]+', ' ', Path(original_filename).stem).strip()
    out_filename = f"{safe_stem}_Extraction.xlsx"
    get_storage().put(file_id, xlsx_bytes, out_filename)

    preview_cols = _unique([flat_label(c, result.band_labels) for c in result.columns])
    preview_rows = [[_jsonify(row.get(c.id)) for c in result.columns] for row in result.rows]

    log.info(
        "pipeline.done",
        rows=len(result.rows),
        tags=result.total_tags,
        format=FORMAT_NAME,
        warnings=len(result.warnings),
    )
    EXTRACTION_ROWS.inc(len(result.rows))

    return {
        "status": "done",
        "file_id": file_id,
        "filename": out_filename,
        "format": f"{FORMAT_NAME}: " + " + ".join(sheet_names),
        "spir_no": result.spir_no,
        "equipment": "",
        "manufacturer": "",
        "supplier": result.vendor.get("vendor_name") or "",
        "spir_type": None,   # the Saipem form has no SPIR TYPE selection
        "eqpt_qty": result.total_tags,
        "spare_items": result.spare_items,
        "total_tags": result.total_tags,
        "annexure_count": 0,
        "total_rows": len(result.rows),
        "dup1_count": 0,
        "sap_count": 0,
        "preview_cols": preview_cols,
        "preview_rows": preview_rows,
        "sheet_profiles": [
            {"name": n, "role": "data", "tag_layout": "saipem_matrix"} for n in result.sheets
        ],
        "currency_rates": None,
        "plant": planning_plant.code if planning_plant else None,
        "plant_description": planning_plant.description if planning_plant else None,
        "extraction_mode": MODE_SAIPEM,
        "warnings": result.warnings,
    }


def _unique(labels: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for lab in labels:
        n = seen.get(lab, 0)
        seen[lab] = n + 1
        out.append(lab if n == 0 else f"{lab} ({n + 1})")
    return out


def _jsonify(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)
