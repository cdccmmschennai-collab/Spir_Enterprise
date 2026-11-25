"use client";

import { useCallback, useRef, useState } from "react";
import {
  AlertCircle,
  ArrowRight,
  CloudUpload,
  Download,
  FileSpreadsheet,
  GitCompare,
  Loader2,
  RefreshCw,
  X,
} from "lucide-react";
import { SidebarLayout } from "@/components/sidebar";
import { authHeaders } from "@/lib/auth";
import { cn, formatBytes } from "@/lib/utils";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const ACCEPTED = ".xlsx,.xlsm,.xls";

// ─── Types ─────────────────────────────────────────────────────────────────────

interface FileSide {
  filename: string;
  spir_no: string;
  revision: string | null;
  issue_date: string | null;
  spir_type: string;
  tags: number;
  spares: number;
}

interface CompareResult {
  status: string;
  file_id: string;
  filename: string;
  mode: "REVISION" | "SPIR TYPE";
  swapped: boolean;
  source: FileSide;
  comparison: FileSide;
  tags_common: number;
  tags_added: number;
  tags_deleted: number;
  tags_source_only: number;
  tags_modified: number;
  spares_added: number;
  spares_deleted: number;
  spares_source_only: number;
  spares_modified: number;
  spares_unchanged?: number;
  spares_similar?: number;
  spares_moved: number;
  other_changes: number;
  check_manually: number;
  check_extraction: number;
  possible_renames: number;
}

// ─── File picker ───────────────────────────────────────────────────────────────

interface FilePickerProps {
  label: string;
  hint: string;
  file: File | null;
  onFile: (f: File | null) => void;
  disabled?: boolean;
}

function FilePicker({ label, hint, file, onFile, disabled }: FilePickerProps) {
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
    <div className="flex min-w-0 flex-1 flex-col gap-2">
      <div className="flex items-baseline justify-between px-1">
        <p className="text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">{label}</p>
        <p className="text-[11px] text-slate-400 dark:text-slate-500">{hint}</p>
      </div>
      <div
        role="button"
        tabIndex={disabled ? -1 : 0}
        aria-label={`${label}: ${file ? file.name : "choose a SPIR Excel file"}`}
        onKeyDown={(e) => {
          if (!disabled && (e.key === "Enter" || e.key === " ")) {
            e.preventDefault();
            inputRef.current?.click();
          }
        }}
        onDragOver={(e) => { e.preventDefault(); if (!disabled) setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        onClick={() => !disabled && inputRef.current?.click()}
        className={cn(
          "flex min-h-[200px] cursor-pointer flex-col items-center justify-center gap-4 rounded-2xl border-2 border-dashed px-5 py-8 text-center transition-all duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400/40",
          dragging
            ? "border-brand-400 bg-brand-50 dark:bg-brand-950/30"
            : file
            ? "border-brand-200 bg-white dark:border-brand-800 dark:bg-slate-800"
            : "border-slate-200 bg-white hover:border-brand-300 hover:bg-brand-50/30 dark:border-slate-600 dark:bg-slate-800 dark:hover:border-brand-500 dark:hover:bg-brand-950/20",
          disabled && "cursor-not-allowed opacity-60"
        )}
      >
        {file ? (
          <>
            <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-brand-100 dark:bg-brand-900/50">
              <FileSpreadsheet className="h-6 w-6 text-brand-600 dark:text-brand-400" />
            </div>
            <div className="min-w-0 max-w-full">
              <p className="truncate text-sm font-semibold text-slate-800 dark:text-slate-200" title={file.name}>{file.name}</p>
              <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{formatBytes(file.size)}</p>
            </div>
            {!disabled && (
              <button
                type="button"
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
              "flex h-12 w-12 items-center justify-center rounded-xl transition-colors",
              dragging ? "bg-brand-100 dark:bg-brand-900/50" : "bg-slate-100 dark:bg-slate-700"
            )}>
              <CloudUpload className={cn(
                "h-6 w-6 transition-colors",
                dragging ? "text-brand-600 dark:text-brand-400" : "text-slate-400 dark:text-slate-500"
              )} />
            </div>
            <div>
              <p className="text-sm font-semibold text-slate-700 dark:text-slate-300">Drop the file here, or click to browse</p>
              <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">Supports .xlsx, .xlsm, .xls</p>
            </div>
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
    </div>
  );
}

// ─── Result pieces ─────────────────────────────────────────────────────────────

type Tone = "added" | "deleted" | "modified" | "similar" | "neutral" | "check";

const TONE: Record<Tone, string> = {
  added: "border-yellow-300 bg-yellow-50 dark:border-yellow-700/60 dark:bg-yellow-950/30",
  deleted: "border-red-300 bg-red-50 dark:border-red-800/60 dark:bg-red-950/30",
  modified: "border-sky-300 bg-sky-50 dark:border-sky-800/60 dark:bg-sky-950/30",
  similar: "border-lime-400 bg-lime-50 dark:border-lime-700/60 dark:bg-lime-950/30",
  check: "border-orange-300 bg-orange-50 dark:border-orange-800/60 dark:bg-orange-950/30",
  neutral: "border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-800",
};

function Stat({ label, value, tone }: { label: string; value: number; tone: Tone }) {
  // Colour only when there is something to see — same rule as the SUMMARY sheet.
  const t = value > 0 ? tone : "neutral";
  return (
    <div className={cn("rounded-xl border px-4 py-3 shadow-sm", TONE[t])}>
      <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">{label}</p>
      <p className="mt-0.5 text-lg font-bold leading-tight tabular-nums text-slate-900 dark:text-slate-100">
        {value.toLocaleString()}
      </p>
    </div>
  );
}

function SideCard({ title, side }: { title: string; side: FileSide }) {
  const rows: [string, string][] = [
    ["SPIR NUMBER", side.spir_no || "-"],
    ["REVISION", side.revision ? `REV-${side.revision}` : "-"],
    ["SPIR TYPE", side.spir_type],
    ["ISSUE DATE", side.issue_date ?? "-"],
    ["EQUIPMENT TAGS", side.tags.toLocaleString()],
    ["SPARE PARTS", side.spares.toLocaleString()],
  ];
  return (
    <div className="min-w-0 flex-1 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-700 dark:bg-slate-800">
      <p className="text-[10px] font-bold uppercase tracking-wider text-brand-700 dark:text-brand-400">{title}</p>
      <p className="mt-1 truncate text-sm font-semibold text-slate-800 dark:text-slate-200" title={side.filename}>
        {side.filename}
      </p>
      <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-3 text-sm">
        {rows.map(([k, v]) => (
          <div key={k} className="min-w-0">
            <dt className="text-[10px] font-bold uppercase tracking-wider text-slate-400 dark:text-slate-500">{k}</dt>
            <dd className="mt-0.5 truncate font-semibold text-slate-800 dark:text-slate-200" title={v}>{v}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

// ─── Page ──────────────────────────────────────────────────────────────────────

export default function ComparePage() {
  const [source, setSource] = useState<File | null>(null);
  const [comparison, setComparison] = useState<File | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<CompareResult | null>(null);
  const [downloading, setDownloading] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const sameFile =
    !!source && !!comparison && source.name === comparison.name && source.size === comparison.size;
  const ready = !!source && !!comparison && !sameFile && !loading;

  const handleCompare = useCallback(async () => {
    if (!source || !comparison) return;
    setLoading(true);
    setError(null);
    setResult(null);
    const controller = new AbortController();
    abortRef.current = controller;
    const form = new FormData();
    form.append("source_file", source);
    form.append("comparison_file", comparison);
    try {
      const res = await fetch(`${API_URL}/api/compare`, {
        method: "POST",
        headers: { ...authHeaders() },
        body: form,
        signal: controller.signal,
      });
      if (res.status === 401) {
        setError("Session expired. Please log in again.");
        return;
      }
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError(typeof body.detail === "string" ? body.detail : `Comparison failed (${res.status})`);
        return;
      }
      setResult(body as CompareResult);
    } catch (err) {
      if ((err as Error)?.name !== "AbortError") {
        setError("Could not reach the server. Is the backend running?");
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setLoading(false);
    }
  }, [source, comparison]);

  const handleCancel = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const handleDownload = useCallback(async () => {
    if (!result) return;
    setDownloading(true);
    try {
      const res = await fetch(`${API_URL}/api/download/${result.file_id}`, { headers: authHeaders() });
      if (!res.ok) {
        setError("Download failed. The file may have expired - run the comparison again.");
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

  const handleReset = useCallback(() => {
    setSource(null);
    setComparison(null);
    setResult(null);
    setError(null);
  }, []);

  const merge = result?.mode === "SPIR TYPE";

  return (
    <SidebarLayout>
      <div className="mx-auto max-w-5xl space-y-6 p-4 sm:p-6 lg:p-8">
        {/* Heading */}
        <div className="flex items-start gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-brand-100 dark:bg-brand-900/50">
            <GitCompare className="h-5 w-5 text-brand-700 dark:text-brand-400" />
          </div>
          <div>
            <h1 className="text-lg font-bold text-slate-900 dark:text-white">SPIR Comparison</h1>
            <p className="text-sm text-slate-500 dark:text-slate-400">
              Compare two SPIR files and download the result as Excel: LATEST, ADDED &amp; MODIFIED and SUMMARY.
            </p>
          </div>
        </div>

        {!result && (
          <>
            {/* Two files side by side (stacked on small screens) */}
            <div className="flex flex-col gap-4 md:flex-row md:items-stretch">
              <FilePicker label="Source file" hint="older revision" file={source} onFile={setSource} disabled={loading} />
              <div className="hidden items-center justify-center pt-6 md:flex">
                <ArrowRight className="h-5 w-5 text-slate-300 dark:text-slate-600" />
              </div>
              <FilePicker label="Comparison file" hint="newer revision" file={comparison} onFile={setComparison} disabled={loading} />
            </div>

            {sameFile && (
              <div className="flex items-start gap-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-700 dark:border-amber-800/60 dark:bg-amber-950/30 dark:text-amber-400">
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>Both boxes hold the same file. Choose two different SPIR files.</span>
              </div>
            )}

            {error && (
              <div role="alert" className="flex items-start gap-3 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-400">
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>{error}</span>
              </div>
            )}

            <div className="flex justify-center gap-3">
              {loading ? (
                <>
                  <div className="flex h-11 items-center gap-2 rounded-xl bg-brand-700/90 px-8 text-sm font-semibold text-white shadow-md">
                    <Loader2 className="h-4 w-4 animate-spin" />
                    Comparing…
                  </div>
                  <button
                    type="button"
                    onClick={handleCancel}
                    className="flex h-11 items-center rounded-xl border border-slate-200 bg-white px-5 text-sm font-medium text-slate-600 hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
                  >
                    Cancel
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  onClick={handleCompare}
                  disabled={!ready}
                  className="flex h-11 items-center gap-2 rounded-xl bg-brand-700 px-8 text-sm font-semibold text-white shadow-md shadow-brand-200 transition-all hover:bg-brand-800 disabled:cursor-not-allowed disabled:opacity-50 dark:shadow-none"
                >
                  <GitCompare className="h-4 w-4" />
                  Run Comparison
                </button>
              )}
            </div>
          </>
        )}

        {result && (
          <>
            {/* Outcome bar */}
            <div className="flex flex-col gap-3 rounded-2xl border border-emerald-200 bg-white p-4 shadow-sm sm:flex-row sm:items-center sm:justify-between dark:border-emerald-800/50 dark:bg-slate-800">
              <div className="min-w-0">
                <p className="flex items-center gap-2 text-sm font-semibold text-slate-800 dark:text-slate-200">
                  <span className="rounded-full bg-brand-100 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-brand-700 dark:bg-brand-950 dark:text-brand-300">
                    {result.mode === "REVISION" ? "Revision comparison" : "SPIR type comparison"}
                  </span>
                  <span className="truncate" title={result.filename}>{result.filename}</span>
                </p>
                {result.swapped && (
                  <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                    The files were swapped: {result.source.filename} is older, so it is the source.
                  </p>
                )}
              </div>
              <div className="flex shrink-0 gap-2">
                <button
                  type="button"
                  onClick={handleReset}
                  className="flex h-9 items-center gap-2 rounded-xl border border-slate-200 bg-white px-4 text-sm font-medium text-slate-600 shadow-sm hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
                >
                  <RefreshCw className="h-3.5 w-3.5" />
                  New Comparison
                </button>
                <button
                  type="button"
                  onClick={handleDownload}
                  disabled={downloading}
                  className="flex h-9 items-center gap-2 rounded-xl bg-brand-700 px-4 text-sm font-semibold text-white shadow-md shadow-brand-200 hover:bg-brand-800 disabled:opacity-60 dark:shadow-none"
                >
                  {downloading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
                  Download Comparison
                </button>
              </div>
            </div>

            {error && (
              <div role="alert" className="flex items-start gap-3 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-400">
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>{error}</span>
              </div>
            )}

            {/* Source / comparison details */}
            <div className="flex flex-col gap-4 md:flex-row">
              <SideCard title="Source file" side={result.source} />
              <SideCard title="Comparison file" side={result.comparison} />
            </div>

            {/* Counts — same items and colours as the SUMMARY sheet */}
            <div className="space-y-3">
              <h2 className="px-1 text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">
                {merge ? "Combined spares (different SPIR type)" : "Changes in the comparison file"}
              </h2>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
                <Stat label="Spare parts added" value={result.spares_added} tone="added" />
                {merge ? (
                  <>
                    <Stat label="Spare parts similar (both files)" value={result.spares_similar ?? 0} tone="similar" />
                    <Stat label="Spare parts unchanged (source only)" value={result.spares_source_only} tone="neutral" />
                  </>
                ) : (
                  <>
                    <Stat label="Spare parts deleted" value={result.spares_deleted} tone="deleted" />
                    <Stat label="Spare parts modified" value={result.spares_modified} tone="modified" />
                    <Stat label="Spare parts unchanged" value={result.spares_unchanged ?? 0} tone="neutral" />
                    <Stat label="Spare parts moved to another tag" value={result.spares_moved} tone="modified" />
                    <Stat label="Other cell changes" value={result.other_changes} tone="modified" />
                  </>
                )}
              </div>

              <h2 className="px-1 pt-2 text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">
                Equipment scope
              </h2>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
                <Stat label={merge ? "Similar tags (both files)" : "Common tags (both files)"} value={result.tags_common} tone={merge ? "similar" : "neutral"} />
                <Stat label="Tags added" value={result.tags_added} tone="added" />
                {merge ? (
                  <Stat label="Tags unchanged (source only)" value={result.tags_source_only} tone="neutral" />
                ) : (
                  <>
                    <Stat label="Tags deleted" value={result.tags_deleted} tone="deleted" />
                    <Stat label="Tags modified (make / model / serial / qty)" value={result.tags_modified} tone="modified" />
                  </>
                )}
              </div>

              {(result.check_manually > 0 || result.check_extraction > 0 || result.possible_renames > 0) && (
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
                  <Stat label="Rows to check manually" value={result.check_manually} tone="check" />
                  <Stat label="Rows in file but not extracted" value={result.check_extraction} tone="check" />
                  <Stat label="Possible tag renames" value={result.possible_renames} tone="check" />
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </SidebarLayout>
  );
}
