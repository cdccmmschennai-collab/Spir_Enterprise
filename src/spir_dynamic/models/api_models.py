"""Pydantic response models for the API."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class ExtractResponse(BaseModel):
    status: str = "done"
    format: str = ""
    spir_no: str = ""
    equipment: str = ""
    manufacturer: str = ""
    supplier: str = ""
    spir_type: Optional[str] = None
    eqpt_qty: int = 0
    spare_items: int = 0
    total_tags: int = 0
    annexure_count: int = 0
    total_rows: int = 0
    dup1_count: int = 0
    sap_count: int = 0
    preview_cols: list[str] = []
    preview_rows: list[list[Any]] = []
    file_id: str = ""
    filename: str = ""
    sheet_profiles: list[dict[str, Any]] = []


class HealthResponse(BaseModel):
    status: str = "healthy"
    version: str = ""


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None
