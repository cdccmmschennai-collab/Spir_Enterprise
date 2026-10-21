"""
Saipem SPIR support — a format-specific path kept apart from the generic extractor.

    detector      which sheets are Saipem SPIR forms (structure, not filename)
    layout        labels, merged ranges, header tree → field positions
    equipment     equipment groups, tag expansion, serial pairing
    matrix        spare-part lines and their per-group installed quantities
    extractor     per-tag output rows (generic row convention)
    profile       exact ordered export columns and header hierarchy
    excel_builder Saipem output workbook (merged 3-row header)
    service       pipeline tail called from app/pipeline.run_pipeline
"""
from spir_dynamic.extraction.saipem.detector import detect_saipem_sheets

__all__ = ["detect_saipem_sheets"]
