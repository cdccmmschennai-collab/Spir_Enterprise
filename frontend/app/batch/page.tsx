"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  CloudUpload,
  FileSpreadsheet,
  X,
  Loader2,
  Download,
  AlertCircle,
  CheckCircle2,
  Clock,
  AlertTriangle,
  Files,
  Layers,
  Plus,
  Zap,
  ChevronDown,
  ChevronRight,
  GitMerge,
} from "lucide-react";
import { SidebarLayout } from "@/components/sidebar";
import { authHeaders } from "@/lib/auth";
import { cn } from "@/lib/utils";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const ACCEPTED = ".xlsx,.xlsm,.xls";
const MAX_FILES = 20;
const POLL_MS = 2500;
const SESSION_KEY = "spir_batch_session";
const SESSION_TTL_MS = 2 * 60 * 60 * 1000; // 2 h — matches backend batch_ttl_seconds

// ─── Types ─────────────────────────────────────────────────────────────────────

interface FileResult {
  filename: string;
  status: "pending" | "running" | "ok" | "error";
  total_rows: number;
  total_tags: number;
  spir_no: string;
  file_id: string;
  error: string;
  queue_position: number | null;
}

interface BatchJob {
  job_id: string;
  status: "processing" | "done" | "partial" | "failed";
  total: number;
  completed: number;
  succeeded: number;
  pending_count: number;
  results: FileResult[];
}

type CombineState = "idle" | "combining" | "ready" | "error";

// ─── Session persistence (sessionStorage — clears when browser tab closes) ─────

function saveSession(jobId: string, status: BatchJob): void {
  try {
    sessionStorage.setItem(
      SESSION_KEY,
      JSON.stringify({ jobId, status, savedAt: Date.now() })
    );
  } catch {
    // storage full or unavailable — non-fatal
  }
}

function loadSession(): { jobId: string; status: BatchJob } | null {
  try {
    const raw = sessionStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const { jobId, status, savedAt } = JSON.parse(raw) as {
      jobId: string;
      status: BatchJob;
      savedAt: number;
    };
    if (!jobId || Date.now() - savedAt > SESSION_TTL_MS) {
      sessionStorage.removeItem(SESSION_KEY);
      return null;
    }
    return { jobId, status };
  } catch {
    return null;
  }
}

function clearSession(): void {
  try {
    sessionStorage.removeItem(SESSION_KEY);
  } catch {}
}

// ─── Helpers ───────────────────────────────────────────────────────────────────

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// ─── Status Badge ──────────────────────────────────────────────────────────────

function FileStatusBadge({
  status,
  queuePosition,
}: {
  status: FileResult["status"];
  queuePosition: number | null;
}) {
  if (status === "pending") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full bg-slate-100 dark:bg-slate-700 px-2.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-slate-500 dark:text-slate-400 whitespace-nowrap">
        <Clock className="h-3 w-3 shrink-0" />
        {queuePosition ? `Queued #${queuePosition}` : "Waiting"}
      </span>
    );
  }
  if (status === "running") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-blue-200 bg-blue-50 dark:border-blue-800 dark:bg-blue-950/50 px-2.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-blue-700 dark:text-blue-400 whitespace-nowrap">
        <Loader2 className="h-3 w-3 animate-spin shrink-0" />
        Processing
      </span>
    );
  }
  if (status === "ok") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-200 bg-emerald-50 dark:border-emerald-800 dark:bg-emerald-950/50 px-2.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-emerald-700 dark:text-emerald-400 whitespace-nowrap">
        <CheckCircle2 className="h-3 w-3 shrink-0" />
        Completed
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-red-200 bg-red-50 dark:border-red-800 dark:bg-red-950/50 px-2.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-red-700 dark:text-red-400 whitespace-nowrap">
      <AlertTriangle className="h-3 w-3 shrink-0" />
      Failed
    </span>
  );
}

// ─── Page ──────────────────────────────────────────────────────────────────────

export default function BatchPage() {
  // Prevent SSR/hydration mismatch for sessionStorage reads
  const [hydrated, setHydrated] = useState(false);

  const [files, setFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState<BatchJob | null>(null);
  const [downloading, setDownloading] = useState<string | null>(null);
  const [expandedIdx, setExpandedIdx] = useState<number | null>(null);
  const [combineState, setCombineState] = useState<CombineState>("idle");
  const [combinedFileId, setCombinedFileId] = useState<string | null>(null);

  const inputRef = useRef<HTMLInputElement>(null);
  const pollIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const activeJobIdRef = useRef<string | null>(null);

  // ── Polling ──────────────────────────────────────────────────────────────────

  const stopPolling = useCallback(() => {
    if (pollIntervalRef.current !== null) {
      clearInterval(pollIntervalRef.current);
      pollIntervalRef.current = null;
    }
    activeJobIdRef.current = null;
  }, []);

  useEffect(() => () => stopPolling(), [stopPolling]);

  const startPolling = useCallback(
    (jid: string) => {
      activeJobIdRef.current = jid;

      const poll = async () => {
        if (activeJobIdRef.current !== jid) return; // stale poll after reset
        try {
          const res = await fetch(`${API_URL}/api/batch/${jid}`, {
            headers: authHeaders(),
          });
          if (res.status === 401) {
            stopPolling();
            setError("Session expired. Please log in again.");
            return;
          }
          if (!res.ok) return; // transient server error — keep retrying
          const data: BatchJob = await res.json();
          setJobStatus(data);
          saveSession(jid, data); // persist latest state for navigation recovery
          if (data.status !== "processing") stopPolling();
        } catch {
          // transient network error — keep polling silently
        }
      };

      poll(); // immediate first check
      pollIntervalRef.current = setInterval(poll, POLL_MS);
    },
    [stopPolling]
  );

  // ── Session restore on mount ─────────────────────────────────────────────────

  useEffect(() => {
    const session = loadSession();
    if (session?.jobId) {
      setJobId(session.jobId);
      if (session.status) setJobStatus(session.status);
      // Resume polling only if job was still in-flight when we navigated away
      if (session.status?.status === "processing") {
        startPolling(session.jobId);
      }
    }
    setHydrated(true);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // ── File selection ───────────────────────────────────────────────────────────

  const addFiles = useCallback((incoming: FileList | File[]) => {
    const valid = Array.from(incoming).filter((f) =>
      /\.(xlsx|xlsm|xls)$/i.test(f.name)
    );
    setFiles((prev) => [...prev, ...valid].slice(0, MAX_FILES));
    setError(null);
  }, []);

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragging(false);
      if (uploading || jobId) return;
      addFiles(e.dataTransfer.files);
    },
    [uploading, jobId, addFiles]
  );

  const removeFile = useCallback(
    (idx: number) => setFiles((prev) => prev.filter((_, i) => i !== idx)),
    []
  );

  // ── Upload ───────────────────────────────────────────────────────────────────

  const handleUpload = useCallback(async () => {
    if (files.length === 0) return;
    setUploading(true);
    setError(null);
    setJobStatus(null);
    setCombineState("idle");
    setCombinedFileId(null);
    setExpandedIdx(null);
    stopPolling();
    clearSession();

    const form = new FormData();
    for (const f of files) form.append("files", f);

    try {
      const res = await fetch(`${API_URL}/api/batch/extract`, {
        method: "POST",
        headers: authHeaders(),
        body: form,
      });
      if (res.status === 401) {
        setError("Session expired. Please log in again.");
        return;
      }
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setError(data.detail ?? `Upload failed (${res.status})`);
        return;
      }
      const { job_id } = await res.json();
      setJobId(job_id);
      setFiles([]); // clear selection — files are now queued on the server
      startPolling(job_id);
    } catch {
      setError("Could not reach the server. Is the backend running?");
    } finally {
      setUploading(false);
    }
  }, [files, startPolling, stopPolling]);

  // ── Download ─────────────────────────────────────────────────────────────────
  // Root cause of Excel corruption: r.filename is the original upload name
  // (e.g. test.xlsm) but the server always outputs openpyxl XLSX bytes.
  // Fix: read Content-Disposition header (server always sends *.xlsx filename);
  // fall back to stripping the extension and appending .xlsx client-side.

  const handleDownload = useCallback(
    async (file_id: string, fallbackName: string) => {
      setDownloading(file_id);
      setError(null);
      try {
        const res = await fetch(`${API_URL}/api/download/${file_id}`, {
          headers: authHeaders(),
        });
        if (!res.ok) {
          setError("Download failed. The file may have expired.");
          return;
        }

        // Prefer server filename (always *.xlsx) over the original upload name
        let downloadName = fallbackName;
        const disposition = res.headers.get("Content-Disposition");
        if (disposition) {
          const match = disposition.match(
            /filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/i
          );
          if (match?.[1]) {
            downloadName = match[1].replace(/['"]/g, "").trim();
          }
        }
        // Safety net: normalize any non-xlsx extension
        if (!downloadName.toLowerCase().endsWith(".xlsx")) {
          const stem = downloadName.replace(/\.[^.]+$/, "");
          downloadName = stem ? `${stem}.xlsx` : "Extraction.xlsx";
        }

        // arrayBuffer preserves exact binary bytes — no MIME coercion
        const buffer = await res.arrayBuffer();
        const blob = new Blob([buffer], {
          type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = downloadName;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
      } catch {
        setError("Download failed.");
      } finally {
        setDownloading(null);
      }
    },
    []
  );

  // ── Combine ──────────────────────────────────────────────────────────────────

  const handleCombine = useCallback(async () => {
    if (!jobId || !jobStatus) return;
    const okIds = jobStatus.results
      .filter((r) => r.status === "ok" && r.file_id)
      .map((r) => r.file_id);
    if (okIds.length < 2) return;

    setCombineState("combining");
    setError(null);
    try {
      const res = await fetch(`${API_URL}/api/batch/${jobId}/combine`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ file_ids: okIds }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setError(
          data.detail ??
            "Combine failed. Row data may have expired (2 h TTL)."
        );
        setCombineState("error");
        return;
      }
      const data = await res.json();
      setCombinedFileId(data.file_id);
      setCombineState("ready");
    } catch {
      setError("Combine failed — server unreachable.");
      setCombineState("error");
    }
  }, [jobId, jobStatus]);

  // ── Reset ────────────────────────────────────────────────────────────────────

  const handleReset = useCallback(() => {
    stopPolling();
    clearSession();
    setJobId(null);
    setJobStatus(null);
    setFiles([]);
    setError(null);
    setDownloading(null);
    setExpandedIdx(null);
    setCombineState("idle");
    setCombinedFileId(null);
  }, [stopPolling]);

  // ── Derived state ─────────────────────────────────────────────────────────────

  const isJobDone = jobStatus && jobStatus.status !== "processing";
  const processingFile = jobStatus?.results.find((r) => r.status === "running");
  const failedCount = jobStatus
    ? jobStatus.completed - jobStatus.succeeded
    : 0;
  const progressPct = jobStatus
    ? Math.round((jobStatus.completed / Math.max(jobStatus.total, 1)) * 100)
    : 0;
  const canCombine = isJobDone && (jobStatus?.succeeded ?? 0) > 1;

  // ── Render ───────────────────────────────────────────────────────────────────

  if (!hydrated) return <SidebarLayout><></></SidebarLayout>;

  return (
    <SidebarLayout>
      <div className="mx-auto max-w-4xl space-y-6 p-4 sm:p-6 lg:p-8">

        {/* ── Page header ── */}
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-violet-100 dark:bg-violet-900/50">
              <Layers className="h-5 w-5 text-violet-600 dark:text-violet-400" />
            </div>
            <div>
              <h1 className="text-lg font-bold text-slate-900 dark:text-white">
                Batch Extraction
              </h1>
              <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
                Upload multiple SPIR files - queued and processed one by one
              </p>
            </div>
          </div>
          {jobId && (
            <button
              onClick={handleReset}
              className="flex items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-600 shadow-sm transition-colors hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
            >
              <Plus className="h-3.5 w-3.5" />
              New Batch
            </button>
          )}
        </div>

        {/* ── Upload section (hidden while a job is active) ── */}
        {!jobId && (
          <>
            {/* Drop zone */}
            <div
              onDragOver={(e) => {
                e.preventDefault();
                if (!uploading) setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={handleDrop}
              onClick={() => !uploading && inputRef.current?.click()}
              className={cn(
                "flex cursor-pointer flex-col items-center justify-center gap-4 rounded-2xl border-2 border-dashed px-6 py-12 transition-all duration-200",
                dragging
                  ? "border-violet-400 bg-violet-50 dark:bg-violet-950/30"
                  : "border-slate-200 bg-white hover:border-violet-300 hover:bg-violet-50/30 dark:border-slate-600 dark:bg-slate-800 dark:hover:border-violet-500 dark:hover:bg-violet-950/20",
                uploading && "cursor-not-allowed opacity-60"
              )}
            >
              <div
                className={cn(
                  "flex h-14 w-14 items-center justify-center rounded-2xl transition-colors",
                  dragging
                    ? "bg-violet-100 dark:bg-violet-900/50"
                    : "bg-slate-100 dark:bg-slate-700"
                )}
              >
                {uploading ? (
                  <Loader2 className="h-7 w-7 animate-spin text-violet-600 dark:text-violet-400" />
                ) : (
                  <CloudUpload
                    className={cn(
                      "h-7 w-7 transition-colors",
                      dragging
                        ? "text-violet-600 dark:text-violet-400"
                        : "text-slate-400 dark:text-slate-500"
                    )}
                  />
                )}
              </div>

              <div className="text-center">
                <p className="text-base font-semibold text-slate-700 dark:text-slate-300">
                  {uploading ? "Uploading files to queue…" : "Batch SPIR Upload"}
                </p>
                <p className="mt-1 text-sm text-slate-400 dark:text-slate-500">
                  {uploading
                    ? "Files are being streamed to the extraction queue"
                    : "Drag & drop multiple Excel files, or click to browse"}
                </p>
                <p className="mt-1 text-xs text-slate-300 dark:text-slate-600">
                  Up to {MAX_FILES} files · .xlsx, .xlsm, .xls
                </p>
              </div>

              {!uploading && (
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    inputRef.current?.click();
                  }}
                  className="rounded-xl bg-violet-700 px-5 py-2 text-sm font-semibold text-white shadow-md shadow-violet-200 transition-colors hover:bg-violet-800"
                >
                  Browse Files
                </button>
              )}

              <input
                ref={inputRef}
                type="file"
                accept={ACCEPTED}
                multiple
                className="hidden"
                onChange={(e) => {
                  if (e.target.files) addFiles(e.target.files);
                  e.target.value = "";
                }}
              />
            </div>

            {/* Selected file list */}
            {files.length > 0 && (
              <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-800">
                <div className="flex items-center justify-between border-b border-slate-100 px-5 py-3 dark:border-slate-700">
                  <div className="flex items-center gap-2">
                    <Files className="h-4 w-4 text-slate-400" />
                    <span className="text-sm font-semibold text-slate-700 dark:text-slate-300">
                      {files.length} file{files.length !== 1 ? "s" : ""}{" "}
                      selected
                    </span>
                  </div>
                  <button
                    onClick={() => setFiles([])}
                    className="text-xs text-slate-400 transition-colors hover:text-slate-600 dark:hover:text-slate-200"
                  >
                    Clear all
                  </button>
                </div>
                <ul className="divide-y divide-slate-100 dark:divide-slate-700">
                  {files.map((f, i) => (
                    <li
                      key={`${f.name}-${i}`}
                      className="flex items-center gap-3 px-5 py-3"
                    >
                      <FileSpreadsheet className="h-4 w-4 shrink-0 text-violet-500" />
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm font-medium text-slate-700 dark:text-slate-300">
                          {f.name}
                        </p>
                        <p className="text-xs text-slate-400 dark:text-slate-500">
                          {formatBytes(f.size)}
                        </p>
                      </div>
                      <button
                        onClick={() => removeFile(i)}
                        aria-label={`Remove ${f.name}`}
                        className="text-slate-300 transition-colors hover:text-slate-500 dark:text-slate-600 dark:hover:text-slate-400"
                      >
                        <X className="h-4 w-4" />
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {/* Start extraction button */}
            {files.length > 0 && (
              <div className="flex justify-center">
                <button
                  onClick={handleUpload}
                  disabled={uploading}
                  className="flex h-11 items-center gap-2 rounded-xl bg-violet-700 px-8 text-sm font-semibold text-white shadow-md shadow-violet-200 transition-all hover:bg-violet-800 disabled:opacity-60"
                >
                  {uploading ? (
                    <>
                      <Loader2 className="h-4 w-4 animate-spin" />
                      Uploading…
                    </>
                  ) : (
                    <>
                      <Zap className="h-4 w-4" />
                      Start Extraction Queue ({files.length} file
                      {files.length !== 1 ? "s" : ""})
                    </>
                  )}
                </button>
              </div>
            )}

            {/* Empty state */}
            {files.length === 0 && !uploading && (
              <div className="rounded-2xl border border-slate-200 bg-white p-8 text-center shadow-sm dark:border-slate-700 dark:bg-slate-800">
                <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-2xl bg-slate-100 dark:bg-slate-700">
                  <Files className="h-6 w-6 text-slate-400 dark:text-slate-500" />
                </div>
                <p className="mt-3 text-sm font-semibold text-slate-600 dark:text-slate-300">
                  No files selected
                </p>
                <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">
                  Add Excel files above to start a batch extraction queue.
                </p>
              </div>
            )}
          </>
        )}

        {/* ── Error banner ── */}
        {error && (
          <div className="flex items-start gap-3 rounded-xl border border-red-200 bg-red-50 px-4 py-3.5 text-sm text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-400">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        {/* ── Queue view (visible once job is created) ── */}
        {jobStatus && (
          <>
            {/* Summary card */}
            <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-700 dark:bg-slate-800">
              {/* Stats row */}
              <div className="grid grid-cols-2 gap-4 sm:flex sm:flex-wrap sm:items-center sm:gap-6">
                {/* Total */}
                <div className="flex items-center gap-2.5">
                  <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-slate-100 dark:bg-slate-700">
                    <Files className="h-4 w-4 text-slate-500 dark:text-slate-400" />
                  </div>
                  <div>
                    <p className="text-[10px] font-bold uppercase tracking-wider text-slate-400 dark:text-slate-500">
                      Total
                    </p>
                    <p className="text-xl font-bold tabular-nums leading-tight text-slate-900 dark:text-white">
                      {jobStatus.total}
                    </p>
                  </div>
                </div>

                {/* Completed */}
                <div className="flex items-center gap-2.5">
                  <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-emerald-50 dark:bg-emerald-950/50">
                    <CheckCircle2 className="h-4 w-4 text-emerald-600 dark:text-emerald-400" />
                  </div>
                  <div>
                    <p className="text-[10px] font-bold uppercase tracking-wider text-slate-400 dark:text-slate-500">
                      Completed
                    </p>
                    <p className="text-xl font-bold tabular-nums leading-tight text-emerald-700 dark:text-emerald-400">
                      {jobStatus.succeeded}
                    </p>
                  </div>
                </div>

                {/* Failed — only shown if > 0 */}
                {failedCount > 0 && (
                  <div className="flex items-center gap-2.5">
                    <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-red-50 dark:bg-red-950/50">
                      <AlertTriangle className="h-4 w-4 text-red-500 dark:text-red-400" />
                    </div>
                    <div>
                      <p className="text-[10px] font-bold uppercase tracking-wider text-slate-400 dark:text-slate-500">
                        Failed
                      </p>
                      <p className="text-xl font-bold tabular-nums leading-tight text-red-600 dark:text-red-400">
                        {failedCount}
                      </p>
                    </div>
                  </div>
                )}

                {/* Currently processing indicator */}
                {processingFile && (
                  <div className="col-span-2 flex min-w-0 items-center gap-2 sm:col-span-1 sm:ml-auto">
                    <Loader2 className="h-4 w-4 shrink-0 animate-spin text-blue-500" />
                    <span className="truncate text-sm text-slate-500 dark:text-slate-400">
                      Processing:{" "}
                      <span className="font-semibold text-slate-700 dark:text-slate-300">
                        {processingFile.filename}
                      </span>
                    </span>
                  </div>
                )}

                {/* Terminal state badge */}
                {isJobDone && !processingFile && (
                  <div className="col-span-2 flex items-center gap-1.5 sm:col-span-1 sm:ml-auto">
                    {jobStatus.status === "done" ? (
                      <>
                        <CheckCircle2 className="h-4 w-4 text-emerald-500" />
                        <span className="text-sm font-semibold text-emerald-700 dark:text-emerald-400">
                          All files completed
                        </span>
                      </>
                    ) : jobStatus.status === "partial" ? (
                      <>
                        <AlertTriangle className="h-4 w-4 text-amber-500" />
                        <span className="text-sm font-semibold text-amber-700 dark:text-amber-400">
                          Partial completion
                        </span>
                      </>
                    ) : (
                      <>
                        <AlertTriangle className="h-4 w-4 text-red-500" />
                        <span className="text-sm font-semibold text-red-700 dark:text-red-400">
                          All files failed
                        </span>
                      </>
                    )}
                  </div>
                )}
              </div>

              {/* Progress bar */}
              <div className="mt-5">
                <div className="mb-1.5 flex justify-between text-xs text-slate-400 dark:text-slate-500">
                  <span>
                    {jobStatus.completed} of {jobStatus.total} processed
                  </span>
                  <span className="tabular-nums">{progressPct}%</span>
                </div>
                <div className="h-2 overflow-hidden rounded-full bg-slate-100 dark:bg-slate-700">
                  <div
                    className={cn(
                      "h-2 rounded-full transition-all duration-700 ease-out",
                      jobStatus.status === "done"
                        ? "bg-emerald-500"
                        : jobStatus.status === "failed"
                        ? "bg-red-500"
                        : "bg-violet-500"
                    )}
                    style={{ width: `${progressPct}%` }}
                  />
                </div>
              </div>
            </div>

            {/* Per-file queue rows */}
            <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-800">
              <div className="border-b border-slate-100 px-5 py-3.5 dark:border-slate-700">
                <h2 className="text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">
                  Extraction Queue
                </h2>
              </div>

              <ul className="divide-y divide-slate-100 dark:divide-slate-700">
                {jobStatus.results.map((r, i) => (
                  <li key={`${r.filename}-${i}`}>
                    {/* Main row */}
                    <div
                      className={cn(
                        "flex items-start gap-4 px-5 py-4 transition-colors",
                        r.status === "running" &&
                          "bg-blue-50/40 dark:bg-blue-950/10"
                      )}
                    >
                      {/* Status-coloured file icon */}
                      <div
                        className={cn(
                          "mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg",
                          r.status === "ok"
                            ? "bg-emerald-50 dark:bg-emerald-950/50"
                            : r.status === "error"
                            ? "bg-red-50 dark:bg-red-950/50"
                            : r.status === "running"
                            ? "bg-blue-50 dark:bg-blue-950/50"
                            : "bg-slate-100 dark:bg-slate-700"
                        )}
                      >
                        <FileSpreadsheet
                          className={cn(
                            "h-4 w-4",
                            r.status === "ok"
                              ? "text-emerald-600 dark:text-emerald-400"
                              : r.status === "error"
                              ? "text-red-500 dark:text-red-400"
                              : r.status === "running"
                              ? "text-blue-600 dark:text-blue-400"
                              : "text-slate-400 dark:text-slate-500"
                          )}
                        />
                      </div>

                      {/* File info */}
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm font-semibold text-slate-700 dark:text-slate-300">
                          {r.filename}
                        </p>
                        {r.status === "ok" && (
                          <p className="mt-0.5 text-xs text-slate-400 dark:text-slate-500">
                            {r.total_rows.toLocaleString()} rows ·{" "}
                            {r.total_tags.toLocaleString()} tags
                            {r.spir_no ? ` · ${r.spir_no}` : ""}
                          </p>
                        )}
                        {r.status === "error" && (
                          <p
                            className="mt-0.5 truncate text-xs text-red-500 dark:text-red-400"
                            title={r.error}
                          >
                            {r.error || "Extraction failed"}
                          </p>
                        )}
                        {r.status === "running" && (
                          <p className="mt-0.5 text-xs text-blue-500 dark:text-blue-400">
                            Extracting data…
                          </p>
                        )}
                        {r.status === "pending" && (
                          <p className="mt-0.5 text-xs text-slate-400 dark:text-slate-500">
                            {r.queue_position
                              ? `Position ${r.queue_position} in queue`
                              : "Waiting to start"}
                          </p>
                        )}
                      </div>

                      {/* Right side: badge + actions */}
                      <div className="flex shrink-0 flex-wrap items-center gap-2">
                        <FileStatusBadge
                          status={r.status}
                          queuePosition={r.queue_position}
                        />

                        {/* Details toggle */}
                        {r.status === "ok" && (
                          <button
                            onClick={() =>
                              setExpandedIdx(expandedIdx === i ? null : i)
                            }
                            className="flex items-center gap-0.5 text-[10px] font-semibold text-slate-400 transition-colors hover:text-slate-600 dark:hover:text-slate-200"
                          >
                            {expandedIdx === i ? (
                              <ChevronDown className="h-3.5 w-3.5" />
                            ) : (
                              <ChevronRight className="h-3.5 w-3.5" />
                            )}
                            Details
                          </button>
                        )}

                        {/* Per-file download */}
                        {r.status === "ok" && r.file_id && (
                          <button
                            onClick={() =>
                              handleDownload(r.file_id, r.filename)
                            }
                            disabled={downloading === r.file_id}
                            className="flex shrink-0 items-center gap-1.5 rounded-lg bg-violet-700 px-3 py-1.5 text-xs font-semibold text-white transition-colors hover:bg-violet-800 disabled:opacity-60"
                          >
                            {downloading === r.file_id ? (
                              <Loader2 className="h-3 w-3 animate-spin" />
                            ) : (
                              <Download className="h-3 w-3" />
                            )}
                            Download
                          </button>
                        )}
                      </div>
                    </div>

                    {/* Expandable detail panel */}
                    {expandedIdx === i && r.status === "ok" && (
                      <div className="grid grid-cols-2 gap-4 border-t border-slate-100 bg-slate-50 px-5 py-4 dark:border-slate-700 dark:bg-slate-900/50 sm:grid-cols-4">
                        <div>
                          <p className="text-[10px] font-bold uppercase tracking-wider text-slate-400">
                            Rows
                          </p>
                          <p className="mt-0.5 text-base font-bold tabular-nums text-slate-800 dark:text-slate-200">
                            {r.total_rows.toLocaleString()}
                          </p>
                        </div>
                        <div>
                          <p className="text-[10px] font-bold uppercase tracking-wider text-slate-400">
                            Tags
                          </p>
                          <p className="mt-0.5 text-base font-bold tabular-nums text-slate-800 dark:text-slate-200">
                            {r.total_tags.toLocaleString()}
                          </p>
                        </div>
                        {r.spir_no && (
                          <div className="col-span-2">
                            <p className="text-[10px] font-bold uppercase tracking-wider text-slate-400">
                              SPIR Number
                            </p>
                            <p className="mt-0.5 truncate text-sm font-semibold text-slate-800 dark:text-slate-200">
                              {r.spir_no}
                            </p>
                          </div>
                        )}
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </div>

            {/* ── Combine / Merge section ── */}
            {canCombine && (
              <div className="rounded-2xl border border-violet-200 bg-violet-50 p-5 shadow-sm dark:border-violet-800/50 dark:bg-violet-950/20">
                <div className="flex flex-wrap items-center justify-between gap-4">
                  <div className="flex items-center gap-3">
                    <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-violet-100 dark:bg-violet-900/50">
                      <GitMerge className="h-4 w-4 text-violet-600 dark:text-violet-400" />
                    </div>
                    <div>
                      <p className="text-sm font-semibold text-violet-900 dark:text-violet-200">
                        Merge Successful Extractions
                      </p>
                      <p className="mt-0.5 text-xs text-violet-600 dark:text-violet-400">
                        Combine {jobStatus.succeeded} completed files into one
                        Excel workbook
                      </p>
                    </div>
                  </div>

                  <div className="flex items-center gap-3">
                    {/* Download merged file (shown after combine succeeds) */}
                    {combineState === "ready" && combinedFileId && (
                      <button
                        onClick={() =>
                          handleDownload(
                            combinedFileId,
                            "COMBINED_Extraction.xlsx"
                          )
                        }
                        disabled={downloading === combinedFileId}
                        className="flex items-center gap-1.5 rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-emerald-700 disabled:opacity-60"
                      >
                        {downloading === combinedFileId ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        ) : (
                          <Download className="h-3.5 w-3.5" />
                        )}
                        Download Combined Excel
                      </button>
                    )}

                    {/* Combine trigger button */}
                    {combineState !== "ready" && (
                      <button
                        onClick={handleCombine}
                        disabled={combineState === "combining"}
                        className="flex items-center gap-1.5 rounded-lg bg-violet-700 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-violet-800 disabled:opacity-60"
                      >
                        {combineState === "combining" ? (
                          <>
                            <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            Combining…
                          </>
                        ) : (
                          <>
                            <GitMerge className="h-3.5 w-3.5" />
                            Combine All Successful Files ({jobStatus.succeeded})
                          </>
                        )}
                      </button>
                    )}
                  </div>
                </div>

                {/* Status line */}
                {combineState === "ready" && (
                  <p className="mt-3 text-xs text-emerald-700 dark:text-emerald-400">
                    Combined workbook ready: {jobStatus.succeeded} files merged
                    into one Excel file. Individual downloads remain available
                    above.
                  </p>
                )}
                {combineState === "error" && (
                  <p className="mt-3 text-xs text-red-600 dark:text-red-400">
                    Combine failed. Row data expires after 2 hours - try again
                    or start a new batch.
                  </p>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </SidebarLayout>
  );
}
