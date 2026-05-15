"""
Secure path construction for on-disk file storage.
All filesystem paths MUST be built through safe_path().
"""
from __future__ import annotations
import os
from pathlib import Path


def safe_path(storage_root: Path, *segments: str) -> Path:
    """
    Build a canonical absolute path anchored under storage_root.

    Validates every segment: no separators, no '..' sequences.
    Resolves the final path and confirms it stays inside storage_root.
    Raises ValueError on any violation — callers map this to HTTP 400/403.

    Uses os.path.realpath for normalization so Windows 8.3 short-path vs
    long-path inconsistencies do not cause false traversal rejections.
    """
    resolved_root = Path(os.path.realpath(str(storage_root)))
    for seg in segments:
        _validate_segment(seg)
    candidate = Path(os.path.realpath(str(resolved_root.joinpath(*segments))))
    try:
        candidate.relative_to(resolved_root)
    except ValueError:
        raise ValueError(
            f"Path traversal detected: {candidate!r} escapes root {resolved_root!r}"
        )
    return candidate


def _validate_segment(segment: str) -> None:
    if not segment:
        raise ValueError("Path segment must not be empty")
    if Path(segment).name != segment:
        raise ValueError(f"Path segment contains illegal separators: {segment!r}")
    if ".." in segment:
        raise ValueError(f"Traversal pattern '..' in segment: {segment!r}")


def remove_empty_parents(file_path: Path, stop_at: Path) -> None:
    """
    Remove empty parent directories up to (but not including) stop_at.
    Called after a file is deleted to prune empty {user_id}/{job_id}/ dirs.
    Silently ignores non-empty dirs and dirs outside stop_at.
    """
    for parent in (file_path.parent, file_path.parent.parent):
        if parent == stop_at:
            break
        try:
            if parent.is_relative_to(stop_at):
                parent.rmdir()  # no-op if non-empty — OSError caught below
        except OSError:
            break
