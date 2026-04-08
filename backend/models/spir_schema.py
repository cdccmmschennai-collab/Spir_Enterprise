"""
models/spir_schema.py
──────────────────────
All Pydantic data contracts for the SPIR extraction system.

  ExtractResponse      — returned by POST /extract (sync or async queued)
  JobStatusResponse    — returned by GET /status/{job_id}
  HealthResponse       — returned by GET /health
  NormalizedRow        — internal canonical row after tag-splitting + normalization
"""
from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, Field


# ── Internal normalized row ────────────────────────────────────────────────────

class NormalizedRow(BaseModel):
    """
    Canonical output row produced by the pipeline after:
      • format parsing
      • tag splitting
      • deduplication
      • currency conversion
    """
    tag:           Optional[str]   = None
    description:   Optional[str]   = None
    quantity:      Optional[float] = None
    unit_price:    Optional[float] = None
    currency:      Optional[str]   = None
    unit_price_inr: Optional[float] = None   # after currency conversion
    total_price:   Optional[float] = None
    part_number:   Optional[str]   = None
    manufacturer:  Optional[str]   = None
    supplier:      Optional[str]   = None
    uom:           Optional[str]   = None
    delivery_weeks: Optional[float] = None
    sap_number:    Optional[str]   = None
    classification: Optional[str]  = None
    sheet:         Optional[str]   = None
    format_source: Optional[str]   = None   # which FORMAT detected this row

    def dedup_key(self) -> tuple:
        """Key for duplicate detection: (tag, description)."""
        return (
            (self.tag or "").strip().upper(),
            (self.description or "").strip().upper(),
        )


# ── API response models ────────────────────────────────────────────────────────

class ExtractResponse(BaseModel):
    """
    Returned by POST /extract.
    background=False → processing done, all fields populated.
    background=True  → job queued, poll /status/{job_id}.
    """
    job_id:      str
    status:      str          # 'done' | 'queued' | 'processing' | 'failed'
    background:  bool = False

    # Populated when status == 'done'
    format:         Optional[str]            = None
    spir_no:        Optional[str]            = None
    equipment:      Optional[str]            = None
    manufacturer:   Optional[str]            = None
    supplier:       Optional[str]            = None
    spir_type:      Optional[str]            = None
    eqpt_qty:       Optional[int]            = None
    spare_items:    Optional[int]            = None
    total_tags:     Optional[int]            = None
    annexure_count: Optional[int]            = None
    annexure_stats: Optional[dict[str, int]] = None
    dup1_count:     Optional[int]            = None
    sap_count:      Optional[int]            = None
    total_rows:     Optional[int]            = None
    preview_cols:   Optional[list[str]]           = None
    preview_rows:   Optional[list[list[Any]]]     = None
    file_id:        Optional[str]            = None
    filename:       Optional[str]            = None


class JobStatusResponse(BaseModel):
    """Returned by GET /status/{job_id}."""
    job_id:   str
    status:   str        # 'queued' | 'processing' | 'done' | 'failed'
    progress: int  = 0  # 0–100
    message:  str  = ""
    result:   Optional[ExtractResponse] = None
    error:    Optional[str]             = None


class HealthResponse(BaseModel):
    status:  str   # 'healthy' | 'degraded'
    version: str
    redis:   str   # 'ok' | 'unavailable'
    workers: str   # 'ok' | 'unavailable' | 'no workers'


class ErrorResponse(BaseModel):
    error:  str
    detail: Optional[str] = None
    trace:  Optional[str] = None
