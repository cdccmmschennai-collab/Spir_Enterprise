"use client";

import { useCallback, useEffect, useRef, useState, memo } from "react";
import { useRouter } from "next/navigation";
import {
  CloudUpload,
  FileSpreadsheet,
  X,
  Loader2,
  Download,
  AlertCircle,
  CheckCircle2,
  Hash,
  Tag,
  Layers,
  AlertTriangle,
  BookOpen,
  ChevronLeft,
  ChevronRight,
  ArrowUpRight,
  RefreshCw,
} from "lucide-react";
import { SidebarLayout } from "@/components/sidebar";
import { UploadProgressCard, type UploadStage } from "@/components/upload-progress-card";
import { authHeaders } from "@/lib/auth";
import { cn, formatBytes } from "@/lib/utils";
import { saveSession, loadSession, clearSession, dismissSession } from "@/lib/extraction-session";
import { directUpload, cancelDirectUpload } from "@/lib/direct-upload";
import { postExtract, ExtractRequestCancelled } from "@/lib/extract-request";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const ACCEPTED = ".xlsx,.xlsm,.xls";
const ROWS_PER_PAGE = 10;
// Background (worker) extraction polling. Only files the API routes to the
// heavy/giant queues are polled, so the cap must outlast the giant worker's
// hard time limit (36 min) plus any queue wait — well inside the 2 h job TTL.
const POLL_MS = 2000;
const MAX_POLL_ATTEMPTS = (60 * 60 * 1000) / POLL_MS; // 60 min

// Generation counter for handleExtract runs. Module-level (not a ref) on
// purpose: after a client-side navigation the previous page instance is
// unmounted but its in-flight request continuation still runs in this tab.
// "Start a new extraction" bumps this so that continuation stops touching
// page state and the persisted session — the request itself is left alone.
let extractionRun = 0;

// ─── Types ─────────────────────────────────────────────────────────────────────

interface ExtractResult {
  status: string;
  format: string;
  spir_no: string;
  equipment: string;
  manufacturer: string;
  supplier: string;
  spir_type: string | null;
  eqpt_qty: number;
  spare_items: number;
  total_tags: number;
  annexure_count: number;
  total_rows: number;
  dup1_count: number;
  sap_count: number;
  preview_cols: string[];
  preview_rows: (string | number | null)[][];
  file_id: string;
  filename: string;
}

function normalizeResult(data: Partial<ExtractResult>): ExtractResult {
  return {
    status: "",
    format: "",
    spir_no: "",
    equipment: "",
    manufacturer: "",
    supplier: "",
    spir_type: null,
    eqpt_qty: 0,
    spare_items: 0,
    total_tags: 0,
    annexure_count: 0,
    total_rows: 0,
    dup1_count: 0,
    sap_count: 0,
    preview_cols: [],
    preview_rows: [],
    file_id: "",
    filename: "",
    ...data,
  };
}

// ─── Row status helper ─────────────────────────────────────────────────────────

type RowStatus = "VALID" | "ERROR" | "DUPLICATE";

function getRowStatus(row: (string | number | null)[], cols: string[]): RowStatus {
  const errorIdx = cols.findIndex((c) => c.toUpperCase() === "ERROR");
  if (errorIdx === -1) return "VALID";
  const val = row[errorIdx];
  if (val === null || val === "" || val === 0) return "VALID";
  const s = String(val).toLowerCase();
  if (s.includes("spare duplicate")) return "DUPLICATE";
  return "ERROR";
}

// ─── Upload Zone ───────────────────────────────────────────────────────────────

interface UploadZoneProps {
  file: File | null;
  onFile: (f: File | null) => void;
  disabled?: boolean;
}

function UploadZone({ file, onFile, disabled }: UploadZoneProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragging(false);
      if (disabled) return;
      const dropped = e.dataTransfer.files[0];
      if (dropped) onFile(dropped);
    },
    [disabled, onFile]
  );

  return (
    <div
      onDragOver={(e) => { e.preventDefault(); if (!disabled) setDragging(true); }}
      onDragLeave={() => setDragging(false)}
      onDrop={handleDrop}
      onClick={() => !disabled && inputRef.current?.click()}
      className={cn(
        "flex cursor-pointer flex-col items-center justify-center gap-5 rounded-2xl border-2 border-dashed px-6 py-16 transition-all duration-200",
        dragging
          ? "border-violet-400 bg-violet-50 dark:bg-violet-950/30"
          : "border-slate-200 bg-white hover:border-violet-300 hover:bg-violet-50/30 dark:border-slate-600 dark:bg-slate-800 dark:hover:border-violet-500 dark:hover:bg-violet-950/20",
        disabled && "cursor-not-allowed opacity-60"
      )}
    >
      {file ? (
        <>
          <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-violet-100 dark:bg-violet-900/50">
            <FileSpreadsheet className="h-8 w-8 text-violet-600 dark:text-violet-400" />
          </div>
          <div className="text-center">
            <p className="text-sm font-semibold text-slate-800 dark:text-slate-200">{file.name}</p>
            <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{formatBytes(file.size)} · Ready to extract</p>
          </div>
          {!disabled && (
            <button
              onClick={(e) => { e.stopPropagation(); onFile(null); }}
              className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium text-slate-500 hover:bg-slate-100 hover:text-slate-700 dark:text-slate-400 dark:hover:bg-slate-700 dark:hover:text-slate-200 transition-colors"
            >
              <X className="h-3.5 w-3.5" /> Remove file
            </button>
          )}
        </>
      ) : (
        <>
          <div className={cn(
            "flex h-16 w-16 items-center justify-center rounded-2xl transition-colors",
            dragging ? "bg-violet-100 dark:bg-violet-900/50" : "bg-slate-100 dark:bg-slate-700"
          )}>
            <CloudUpload className={cn(
              "h-8 w-8 transition-colors",
              dragging ? "text-violet-600 dark:text-violet-400" : "text-slate-400 dark:text-slate-500"
            )} />
          </div>
          <div className="text-center">
            <p className="text-base font-semibold text-slate-700 dark:text-slate-300">
              SPIR Excel File Upload
            </p>
            <p className="mt-1 text-sm text-slate-400 dark:text-slate-500">
              Drag and drop your SPIR Excel file here, or click to browse
            </p>
            <p className="mt-1 text-xs text-slate-300 dark:text-slate-600">
              Supports .xlsx, .xlsm, .xls
            </p>
          </div>
          <button
            type="button"
            className="rounded-xl bg-violet-700 px-6 py-2.5 text-sm font-semibold text-white shadow-md shadow-violet-200 hover:bg-violet-800 transition-colors"
            onClick={(e) => { e.stopPropagation(); inputRef.current?.click(); }}
          >
            Browse Files
          </button>
        </>
      )}
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPTED}
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) onFile(f);
          e.target.value = "";
        }}
      />
    </div>
  );
}

// ─── Status Badge ──────────────────────────────────────────────────────────────

function StatusBadge({ status }: { status: RowStatus }) {
  if (status === "VALID") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 dark:bg-emerald-950/50 px-2.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-emerald-700 dark:text-emerald-400 border border-emerald-200 dark:border-emerald-800">
        <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
        VALID
      </span>
    );
  }
  if (status === "DUPLICATE") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-amber-50 dark:bg-amber-950/50 px-2.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-amber-700 dark:text-amber-400 border border-amber-200 dark:border-amber-800">
        <span className="h-1.5 w-1.5 rounded-full bg-amber-500" />
        DUPLICATE
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-red-50 dark:bg-red-950/50 px-2.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-red-700 dark:text-red-400 border border-red-200 dark:border-red-800">
      <span className="h-1.5 w-1.5 rounded-full bg-red-500" />
      ERROR
    </span>
  );
}

// ─── Data Preview Table ─────────────────────────────────────────────────────────

interface PreviewTableProps {
  cols: string[];
  rows: (string | number | null)[][];
  totalRows: number;
}

const PreviewTable = memo(function PreviewTable({ cols, rows, totalRows }: PreviewTableProps) {
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<Set<number>>(new Set());

  const totalPages = Math.max(1, Math.ceil(rows.length / ROWS_PER_PAGE));
  const start = (page - 1) * ROWS_PER_PAGE;
  const pageRows = rows.slice(start, start + ROWS_PER_PAGE);

  // Detect if an error/status column exists so we can append a STATUS badge column
  const errorColIdx = cols.findIndex((c) => c.toUpperCase() === "ERROR" || c.toUpperCase() === "STATUS");

  function toggleAll() {
    if (selected.size === pageRows.length) {
      setSelected(new Set());
    } else {
      setSelected(new Set(pageRows.map((_, i) => start + i)));
    }
  }

  const colCount = cols.length + 2; // +1 checkbox, status badge appended if error col exists

  return (
    <div className="space-y-3">
      <div className="overflow-x-auto rounded-xl border border-slate-200 dark:border-slate-700 shadow-sm">
        <table className="min-w-full text-xs">
          <thead>
            <tr className="bg-slate-50 dark:bg-slate-800 border-b border-slate-200 dark:border-slate-700">
              {/* Checkbox */}
              <th className="w-10 px-3 py-3 sticky left-0 bg-slate-50 dark:bg-slate-800">
                <input
                  type="checkbox"
                  checked={selected.size === pageRows.length && pageRows.length > 0}
                  onChange={toggleAll}
                  className="h-3.5 w-3.5 rounded border-slate-300 text-violet-600 focus:ring-violet-500"
                />
              </th>
              {/* All actual Excel column headers */}
              {cols.map((col) => (
                <th
                  key={col}
                  className="whitespace-nowrap px-4 py-3 text-left font-semibold text-slate-600 dark:text-slate-400 uppercase tracking-wide text-[10px]"
                >
                  {col}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 dark:divide-slate-700 bg-white dark:bg-slate-900">
            {pageRows.length === 0 ? (
              <tr>
                <td colSpan={colCount} className="px-4 py-8 text-center text-slate-400 dark:text-slate-500">
                  No data rows
                </td>
              </tr>
            ) : (
              pageRows.map((row, ri) => {
                const globalIdx = start + ri;
                const isSelected = selected.has(globalIdx);
                const status = getRowStatus(row, cols);

                return (
                  <tr
                    key={ri}
                    className={cn(
                      "transition-colors hover:bg-slate-50 dark:hover:bg-slate-800",
                      isSelected && "bg-violet-50/40 dark:bg-violet-950/20"
                    )}
                  >
                    {/* Checkbox */}
                    <td className="w-10 px-3 py-2.5 sticky left-0 bg-inherit">
                      <input
                        type="checkbox"
                        checked={isSelected}
                        onChange={() => {
                          const next = new Set(selected);
                          if (isSelected) next.delete(globalIdx);
                          else next.add(globalIdx);
                          setSelected(next);
                        }}
                        className="h-3.5 w-3.5 rounded border-slate-300 text-violet-600 focus:ring-violet-500"
                      />
                    </td>
                    {/* All actual cell values */}
                    {cols.map((col, ci) => (
                      <td
                        key={col}
                        className="max-w-[180px] truncate px-4 py-2.5 text-slate-700 dark:text-slate-300"
                        title={row[ci] != null ? String(row[ci]) : ""}
                      >
                        {row[ci] != null && row[ci] !== "" ? String(row[ci]) : (
                          <span className="text-slate-300 dark:text-slate-600">—</span>
                        )}
                      </td>
                    ))}
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {/* Pagination footer */}
      <div className="flex items-center justify-between px-1">
        <p className="text-xs text-slate-500 dark:text-slate-400">
          {totalRows.toLocaleString()} Total Entries · {rows.length} preview rows · {cols.length} columns
        </p>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page === 1}
            className="flex h-7 w-7 items-center justify-center rounded-lg border border-slate-200 dark:border-slate-700 text-slate-500 dark:text-slate-400 hover:bg-slate-50 dark:hover:bg-slate-800 disabled:opacity-40 transition-colors"
          >
            <ChevronLeft className="h-3.5 w-3.5" />
          </button>
          <span className="text-xs font-medium text-slate-600 dark:text-slate-400">
            {page} / {totalPages}
          </span>
          <button
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page === totalPages}
            className="flex h-7 w-7 items-center justify-center rounded-lg border border-slate-200 dark:border-slate-700 text-slate-500 dark:text-slate-400 hover:bg-slate-50 dark:hover:bg-slate-800 disabled:opacity-40 transition-colors"
          >
            <ChevronRight className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    </div>
  );
});

// ─── Page ──────────────────────────────────────────────────────────────────────

export default function ExtractionPage() {
  const router = useRouter();
  const [file, setFile] = useState<File | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ExtractResult | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [savedFilename, setSavedFilename] = useState("");
  // Size from the persisted session — the File object is gone after a remount.
  const [savedSize, setSavedSize] = useState<number | null>(null);
  const [hydrated, setHydrated] = useState(false);
  const [recoveredToHistory, setRecoveredToHistory] = useState(false);
  // True while the API has handed this extraction to a background worker
  // (large file). Kept for session restore; the card itself never shows it.
  const [backgroundJob, setBackgroundJob] = useState(false);
  // Real bytes on the wire for either upload path: the direct browser->storage
  // transfer (Phase 3D) or the classic /api/extract request. "finalizing" is
  // the direct path's server-side verification (cancel no longer possible).
  // null once the body has been delivered — the file is then "processing".
  const [upload, setUpload] = useState<{ loaded: number; total: number; phase: "uploading" | "finalizing" } | null>(null);
  const uploadAbortRef = useRef<AbortController | null>(null);

  // Async polling refs — stable across renders, cleaned up on unmount.
  // The token is a fresh object per polling run so that a stale in-flight
  // poll (reset, or a Strict Mode remount) is dropped even for the same job.
  const pollIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const activePollRef = useRef<object | null>(null);

  const stopPolling = useCallback(() => {
    if (pollIntervalRef.current !== null) {
      clearInterval(pollIntervalRef.current);
      pollIntervalRef.current = null;
    }
    activePollRef.current = null;
  }, []);

  // Clean up polling interval on unmount
  useEffect(() => () => stopPolling(), [stopPolling]);

  const startPolling = useCallback((job_id: string) => {
    stopPolling();
    const token = {};
    activePollRef.current = token;
    let attempts = 0;

    const poll = async () => {
      if (activePollRef.current !== token) return; // stale poll after reset
      try {
        const res = await fetch(`${API_URL}/api/batch/${job_id}/result`, {
          headers: authHeaders(),
        });
        if (activePollRef.current !== token) return; // reset while in flight
        if (res.status === 401) {
          stopPolling();
          clearSession();
          setError("Session expired. Please log in again.");
          setLoading(false);
          return;
        }
        if (!res.ok) {
          stopPolling();
          clearSession();
          setError(`Polling failed (${res.status}). Please try again.`);
          setLoading(false);
          return;
        }
        const data = await res.json();
        if (activePollRef.current !== token) return;
        if (data.status === "done") {
          stopPolling();
          const extracted = normalizeResult(data as Partial<ExtractResult>);
          setResult(extracted);
          saveSession({ status: "complete", filename: extracted.filename, size: loadSession()?.size, savedAt: Date.now(),
            result: { ...extracted, preview_rows: extracted.preview_rows.slice(0, 200) } });
          window.dispatchEvent(new CustomEvent("profile-refresh"));
          setLoading(false);
        } else if (data.status === "error") {
          stopPolling();
          clearSession();
          setError(data.error ?? "Extraction failed. Please try again.");
          setLoading(false);
        } else {
          // still processing
          attempts++;
          if (attempts > MAX_POLL_ATTEMPTS) {
            stopPolling();
            clearSession();
            setError("Extraction timed out. The file may be too large or complex.");
            setLoading(false);
          }
        }
      } catch {
        // transient network error during poll — keep retrying silently
      }
    };

    poll(); // immediate first check
    pollIntervalRef.current = setInterval(poll, POLL_MS);
  }, [stopPolling]);

  // Sync (in-process) request recovery. The classic /api/extract request has
  // no job_id: after a remount the only durable evidence that it finished is
  // (a) the session flipping to "complete" — the original request's
  // continuation still runs in this tab if the user only navigated within
  // the app — or (b) a matching History entry. Checked on the same cadence
  // as worker polling, with the same cap, so the spinner cannot stick forever.
  const startSyncRecovery = useCallback((filename: string, savedAt: number) => {
    stopPolling();
    const token = {};
    activePollRef.current = token;
    let attempts = 0;

    const check = async () => {
      if (activePollRef.current !== token) return;
      const session = loadSession();
      if (session?.status === "complete" && session.result && !session.dismissed) {
        stopPolling();
        setResult(normalizeResult(session.result as Partial<ExtractResult>));
        setLoading(false);
        setSavedFilename("");
        return;
      }
      try {
        const res = await fetch(`${API_URL}/api/history`, { headers: authHeaders() });
        if (activePollRef.current !== token) return;
        if (!res.ok) return; // transient — keep checking
        const items = (await res.json()) as Array<{ filename: string; created_at: string }>;
        if (activePollRef.current !== token) return;
        const completed = items.some(
          (item) =>
            item.filename === filename &&
            new Date(item.created_at).getTime() >= savedAt - 10_000
        );
        if (completed) {
          stopPolling();
          clearSession();
          setLoading(false);
          setSavedFilename("");
          setRecoveredToHistory(true);
          return;
        }
      } catch {
        // Network error — keep checking; "Start a new extraction" is the escape hatch
      }
      attempts++;
      if (attempts > MAX_POLL_ATTEMPTS) {
        stopPolling();
        clearSession();
        setError("Extraction timed out. The file may be too large or complex.");
        setLoading(false);
      }
    };

    check();
    pollIntervalRef.current = setInterval(check, POLL_MS);
  }, [stopPolling]);

  useEffect(() => {
    const session = loadSession();
    // Intentionally left behind via "Start a new extraction": the job (if any)
    // keeps running server-side and shows up in History, but the page starts
    // fresh. Restoring it here would trap the user in the old extraction.
    if (session?.dismissed) {
      setHydrated(true);
      return;
    }
    if (session?.status === "complete" && session.result) {
      setResult(normalizeResult(session.result as Partial<ExtractResult>));
      setSavedSize(session.size ?? null);
      setHydrated(true);
      return;
    }
    if (session?.status === "loading") {
      if (session.phase === "uploading" && session.job_id) {
        // The page went away mid direct-upload: the File object is gone, so
        // the transfer cannot resume. Tell the server to discard the pieces.
        cancelDirectUpload(session.job_id);
        clearSession();
        setError(`The upload of ${session.filename} was interrupted. Please select the file and run the extraction again.`);
        setHydrated(true);
        return;
      }
      setSavedFilename(session.filename);
      setSavedSize(session.size ?? null);
      setLoading(true);

      if (session.job_id) {
        // Background job (large file) — resume polling the in-flight job.
        // Nothing is re-uploaded or re-queued; the job_id is the whole handle.
        setBackgroundJob(true);
        startPolling(session.job_id);
      } else {
        startSyncRecovery(session.filename, session.savedAt);
      }
    }
    setHydrated(true);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const handleExtract = useCallback(async () => {
    if (!file) return;
    setLoading(true);
    setError(null);
    setResult(null);
    setBackgroundJob(false);
    setSavedSize(null);
    stopPolling();

    // This run owns the page until "Start a new extraction" (or a reset)
    // bumps the counter. Everything after an await checks `live()` first so
    // an extraction the user walked away from can finish server-side without
    // overwriting the next one's state or session.
    const run = ++extractionRun;
    const live = () => extractionRun === run;
    const base = { filename: file.name, size: file.size };

    saveSession({ status: "loading", ...base, savedAt: Date.now() });

    // Large files (Phase 3D): the API only plans the upload; the browser
    // writes the workbook straight to storage, then the server verifies it
    // and queues the same background worker. The API answers "api" for
    // small files, and the classic request below runs unchanged.
    const controller = new AbortController();
    uploadAbortRef.current = controller;
    const releaseController = () => {
      if (uploadAbortRef.current === controller) uploadAbortRef.current = null;
    };
    // Nothing has been submitted until the upload plan comes back, so this
    // window is "uploading" (0 bytes, cancellable) — not "processing", where
    // "Start a new extraction" would leave an extraction that never existed.
    setUpload({ loaded: 0, total: file.size, phase: "uploading" });
    const outcome = await directUpload(file, {
      signal: controller.signal,
      onJob: (job_id) => {
        if (live()) saveSession({ status: "loading", ...base, savedAt: Date.now(), job_id, phase: "uploading" });
      },
      onPhase: (phase) => {
        if (live()) setUpload((u) => ({ loaded: u?.loaded ?? 0, total: file.size, phase }));
      },
      onProgress: (loaded, total) => {
        if (live()) setUpload((u) => ({ loaded, total, phase: u?.phase ?? "uploading" }));
      },
    });
    if (!live()) return;
    setUpload(null);
    if (outcome.kind === "queued") {
      releaseController();
      saveSession({ status: "loading", ...base, savedAt: Date.now(),
        job_id: outcome.response.job_id, phase: "processing" });
      setBackgroundJob(true);
      startPolling(outcome.response.job_id);
      return; // loading stays true — cleared by poll when done
    }
    if (outcome.kind === "cancelled") {
      releaseController();
      clearSession();
      setLoading(false);
      return;
    }
    if (outcome.kind === "error") {
      releaseController();
      clearSession();
      setError(outcome.status === 401 ? "Session expired. Please log in again." : outcome.message);
      setLoading(false);
      return;
    }

    // One entry point for every size. The API decides after the upload:
    //   200 + result  → extracted in-process (small file), shown immediately
    //   202 + job_id  → handed to a background worker (large file), polled
    // Sent via XHR (same request) so the card can show real bytes and switch
    // to "processing" once the body has been delivered.
    const form = new FormData();
    form.append("file", file);
    setUpload({ loaded: 0, total: file.size, phase: "uploading" });
    try {
      const res = await postExtract(form, {
        signal: controller.signal,
        onProgress: (loaded, total) => { if (live()) setUpload({ loaded, total, phase: "uploading" }); },
        onSent: () => { if (live()) setUpload(null); },
      });
      if (!live()) return;
      releaseController();
      setUpload(null);
      if (res.status === 401) {
        clearSession();
        setError("Session expired. Please log in again.");
        setLoading(false);
        return;
      }
      if (res.status < 200 || res.status >= 300) {
        clearSession();
        setError(typeof res.body.detail === "string" ? res.body.detail : `Extraction failed (${res.status})`);
        setLoading(false);
        return;
      }
      const payload = res.body;
      if (res.status === 202 && payload.status === "queued" && typeof payload.job_id === "string") {
        // Large file — the worker runs the same pipeline; poll until done.
        saveSession({ status: "loading", ...base, savedAt: Date.now(), job_id: payload.job_id, phase: "processing" });
        setBackgroundJob(true);
        startPolling(payload.job_id);
        return; // loading stays true — cleared by poll when done
      }
      const data = normalizeResult(payload as Partial<ExtractResult>);
      setResult(data);
      // Cap preview_rows before saving to localStorage — full rows can be
      // 2–10 MB for large SPIRs, exceeding the 5–10 MB quota. The download
      // still contains all rows; 200 preview rows = 20 paginated pages.
      saveSession({ status: "complete", filename: data.filename, size: file.size, savedAt: Date.now(),
        result: { ...data, preview_rows: data.preview_rows.slice(0, 200) } });
      window.dispatchEvent(new CustomEvent("profile-refresh"));
      setLoading(false);
    } catch (err) {
      if (!live()) return;
      releaseController();
      setUpload(null);
      clearSession();
      if (!(err instanceof ExtractRequestCancelled)) {
        setError("Could not reach the server. Is the backend running?");
      }
      setLoading(false);
    }
  }, [file, startPolling, stopPolling]);

  const handleDownload = useCallback(async () => {
    if (!result) return;
    setDownloading(true);
    try {
      const res = await fetch(`${API_URL}/api/download/${result.file_id}`, {
        headers: authHeaders(),
      });

      if (!res.ok) {
        setError("Download failed. The file may have expired.");
        return;
      }

      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = result.filename;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      setError("Download failed.");
    } finally {
      setDownloading(false);
    }
  }, [result]);

  // Back to file selection. Only the page's display state is touched: no
  // cancel/delete call is made, and any upload controller is released, not
  // aborted, so a request already handed to the server carries on.
  const resetDisplay = useCallback(() => {
    extractionRun++;
    stopPolling();
    uploadAbortRef.current = null;
    setFile(null);
    setResult(null);
    setError(null);
    setLoading(false);
    setUpload(null);
    setSavedFilename("");
    setSavedSize(null);
    setRecoveredToHistory(false);
    setBackgroundJob(false);
  }, [stopPolling]);

  // Completed / failed: nothing is running, so the session can simply go.
  const handleReset = useCallback(() => {
    clearSession();
    resetDisplay();
  }, [resetDisplay]);

  // Processing: leave the submitted extraction running (it still lands in
  // History) and stop showing it here. The session is kept but flagged so the
  // next mount does not restore it and trap the user in the old extraction.
  const handleStartNew = useCallback(() => {
    dismissSession();
    resetDisplay();
  }, [resetDisplay]);

  // Cancel an in-flight direct upload (parts are discarded server-side).
  const handleCancelUpload = useCallback(() => {
    uploadAbortRef.current?.abort();
  }, []);

  // Leaving the page kills the transfer — let the browser warn first.
  useEffect(() => {
    if (!upload) return;
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [upload]);

  if (!hydrated) {
    return <SidebarLayout><></></SidebarLayout>;
  }

  // Unified stage: both upload paths land on the same card. Bytes in flight →
  // uploading; anything else in flight (sync wait, worker poll, restored
  // session) → processing. "cancelled" is not a stage of its own — the page
  // simply returns to the select-file state.
  const stage: UploadStage | null = loading
    ? upload ? "uploading" : "processing"
    : error ? "failed" : null;
  const activeFilename = file?.name ?? savedFilename;

  return (
    <SidebarLayout>
      {/* ── Dashboard / Upload State ── */}
      {!result && (
        <div className="mx-auto max-w-3xl space-y-6 p-4 sm:p-6 lg:p-8">
          {/* Recovery banner — extraction completed on backend while page was away */}
          {recoveredToHistory && !loading && (
            <div className="flex items-start gap-3 rounded-xl border border-emerald-200 dark:border-emerald-900/50 bg-emerald-50 dark:bg-emerald-950/30 px-4 py-3.5 text-sm text-emerald-700 dark:text-emerald-400">
              <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
              <span>
                Your file finished processing while you were away.{" "}
                Check{" "}
                <button
                  onClick={() => setRecoveredToHistory(false)}
                  className="underline hover:no-underline"
                >
                  History
                </button>{" "}
                for results.
              </span>
            </div>
          )}

          {/* One card for every path: uploading → processing → (results view) / failed.
              Restored sessions (file object gone after reload) use the saved name. */}
          {stage ? (
            <UploadProgressCard
              stage={stage}
              filename={activeFilename}
              size={file?.size ?? savedSize}
              loaded={upload?.loaded}
              total={upload?.total}
              message={stage === "failed" ? error ?? undefined : undefined}
              onCancel={
                stage === "uploading" && upload?.phase === "uploading"
                  ? handleCancelUpload // Cancel: aborts the transfer
                  : stage === "processing"
                  ? handleStartNew // Start a new extraction: leaves the job running
                  : undefined
              }
              cancelLabel={stage === "processing" ? "Start a new extraction" : "Cancel"}
              onRetry={stage === "failed" && file ? handleExtract : undefined}
              onReset={stage === "failed" ? handleReset : undefined}
            />
          ) : (
            <>
              <UploadZone file={file} onFile={setFile} />

              {/* Large-file advisory — shown before extraction starts */}
              {file && file.size > 500 * 1024 * 1024 && (
                <div className="flex items-start gap-3 rounded-xl border border-amber-200 dark:border-amber-800/60 bg-amber-50 dark:bg-amber-950/30 px-4 py-3.5 text-sm text-amber-700 dark:text-amber-400">
                  <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                  <span>
                    <span className="font-semibold">This is a large file ({formatBytes(file.size)}).</span>{" "}
                    Processing may take 10–30 minutes. Once the upload finishes you can continue
                    working and check the result in History.
                  </span>
                </div>
              )}

              {/* Extract button */}
              {file && (
                <div className="flex justify-center">
                  <button
                    onClick={handleExtract}
                    className="flex h-11 items-center gap-2 rounded-xl bg-violet-700 px-8 text-sm font-semibold text-white shadow-md shadow-violet-200 transition-all hover:bg-violet-800"
                  >
                    <FileSpreadsheet className="h-4 w-4" />
                    Run Extraction
                  </button>
                </div>
              )}
            </>
          )}

          {/* System Guide card */}
          <div className="rounded-2xl border border-emerald-200 dark:border-emerald-800/50 bg-white dark:bg-slate-800 p-5 shadow-sm flex items-center gap-4">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-emerald-50 dark:bg-emerald-950/50">
              <BookOpen className="h-4.5 w-4.5 h-[18px] w-[18px] text-emerald-600 dark:text-emerald-400" />
            </div>
            <div className="flex-1 min-w-0">
              <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-200">System Guide</h3>
              <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
                Step-by-step workflow: upload, extract, combine, download.
              </p>
            </div>
            <button
              onClick={() => router.push("/guide")}
              className="flex shrink-0 items-center gap-1.5 text-xs font-semibold text-emerald-700 hover:text-emerald-800 dark:text-emerald-400 dark:hover:text-emerald-300 transition-colors"
            >
              Open Guide
              <ArrowUpRight className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      )}

      {/* ── Extraction Results State ── */}
      {result && (
        <div className="mx-auto max-w-6xl space-y-6 p-4 sm:p-6 lg:p-8">
          {/* Completed state — same card as uploading/processing, with the result actions */}
          <UploadProgressCard
            stage="completed"
            filename={result.filename || file?.name || ""}
            size={file?.size ?? savedSize}
            actions={
              <>
                <button
                  onClick={handleReset}
                  className="flex h-9 items-center gap-2 rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 px-4 text-sm font-medium text-slate-600 dark:text-slate-300 shadow-sm hover:bg-slate-50 dark:hover:bg-slate-700 transition-colors"
                >
                  <RefreshCw className="h-3.5 w-3.5" />
                  New File
                </button>
                <button
                  onClick={handleDownload}
                  disabled={downloading}
                  className="flex h-9 items-center gap-2 rounded-xl bg-violet-700 px-4 text-sm font-semibold text-white shadow-md shadow-violet-200 hover:bg-violet-800 transition-colors disabled:opacity-60"
                >
                  {downloading ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Download className="h-3.5 w-3.5" />
                  )}
                  Download Results
                </button>
              </>
            }
          />

          {/* Stats row */}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {[
              {
                icon: Hash,
                label: "TOTAL ROWS",
                value: result.total_rows.toLocaleString(),
                iconClass: "text-violet-600",
                bgClass: "bg-violet-50 dark:bg-violet-950/50",
              },
              {
                icon: Tag,
                label: "TAGS",
                value: result.total_tags.toLocaleString(),
                iconClass: "text-blue-600",
                bgClass: "bg-blue-50 dark:bg-blue-950/50",
              },
              {
                icon: Layers,
                label: "SPARE ITEMS",
                value: result.spare_items.toLocaleString(),
                iconClass: "text-emerald-600",
                bgClass: "bg-emerald-50 dark:bg-emerald-950/50",
              },
              {
                icon: AlertTriangle,
                label: "DUPLICATES",
                value: result.dup1_count.toLocaleString(),
                iconClass: result.dup1_count > 0 ? "text-red-500" : "text-slate-400",
                bgClass: result.dup1_count > 0 ? "bg-red-50 dark:bg-red-950/50" : "bg-slate-50 dark:bg-slate-700",
                warn: result.dup1_count > 0,
              },
            ].map(({ icon: Icon, label, value, iconClass, bgClass, warn }) => (
              <div
                key={label}
                className={cn(
                  "flex items-center gap-3 rounded-xl border px-4 py-3 shadow-sm bg-white dark:bg-slate-800",
                  warn ? "border-red-200 dark:border-red-900/50" : "border-slate-200 dark:border-slate-700"
                )}
              >
                <div className={cn("flex h-9 w-9 shrink-0 items-center justify-center rounded-lg", bgClass)}>
                  <Icon className={cn("h-4 w-4", iconClass)} />
                </div>
                <div>
                  <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400 dark:text-slate-500">{label}</p>
                  <p className={cn("text-lg font-bold leading-tight", warn ? "text-red-600" : "text-slate-900 dark:text-slate-100")}>
                    {value}
                  </p>
                </div>
                {warn && <AlertTriangle className="ml-auto h-4 w-4 text-red-400" />}
              </div>
            ))}
          </div>

          {/* SPIR Metadata */}
          <div className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-5 shadow-sm">
            <h2 className="mb-4 text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">
              SPIR Metadata
            </h2>
            <div className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4 text-sm">
              {[
                { label: "SPIR NUMBER",  value: result.spir_no },
                { label: "EQUIPMENT",    value: result.equipment },
                { label: "MANUFACTURER", value: result.manufacturer },
                { label: "SUPPLIER",     value: result.supplier },
                { label: "SPIR TYPE",    value: result.spir_type },
                { label: "EQPT QTY",     value: result.eqpt_qty || null },
                { label: "ANNEXURE",     value: result.annexure_count > 0 ? result.annexure_count : null },
              ].map(({ label, value }) =>
                value ? (
                  <div key={label}>
                    <p className="text-[10px] font-bold uppercase tracking-wider text-slate-400 dark:text-slate-500">
                      {label}
                    </p>
                    <p className="mt-0.5 truncate font-semibold text-slate-800 dark:text-slate-200">
                      {String(value)}
                    </p>
                  </div>
                ) : null
              )}
            </div>
          </div>

          {/* Download error */}
          {error && (
            <div className="flex items-start gap-3 rounded-xl border border-red-200 dark:border-red-900/50 bg-red-50 dark:bg-red-950/30 px-4 py-3 text-sm text-red-700 dark:text-red-400">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>{error}</span>
            </div>
          )}

          {/* Data Preview */}
          <div className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-5 shadow-sm">
            <div className="mb-4 flex items-center justify-between">
              <div>
                <h2 className="text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">
                  Data Preview
                </h2>
                <p className="mt-0.5 text-xs text-slate-400 dark:text-slate-500">
                  Showing preview of {result.preview_rows.length} rows · {result.preview_cols.length} columns total
                </p>
              </div>
            </div>
            <PreviewTable
              cols={result.preview_cols}
              rows={result.preview_rows}
              totalRows={result.total_rows}
            />
          </div>
        </div>
      )}

    </SidebarLayout>
  );
}
