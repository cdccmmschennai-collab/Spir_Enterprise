"use client";

import { useCallback, useRef, useState } from "react";
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
  History,
  BookOpen,
  ChevronLeft,
  ChevronRight,
  ArrowUpRight,
  RefreshCw,
} from "lucide-react";
import { SidebarLayout } from "@/components/sidebar";
import { authHeaders } from "@/lib/auth";
import { cn } from "@/lib/utils";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const ACCEPTED = ".xlsx,.xlsm,.xls";
const ROWS_PER_PAGE = 10;

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

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
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
              Supports .xlsx, .xlsm, .xls · Max 2 GB
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

function PreviewTable({ cols, rows, totalRows }: PreviewTableProps) {
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
}

// ─── Page ──────────────────────────────────────────────────────────────────────

export default function ExtractionPage() {
  const [file, setFile] = useState<File | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ExtractResult | null>(null);
  const [downloading, setDownloading] = useState(false);

  async function handleExtract() {
    if (!file) return;
    setLoading(true);
    setError(null);
    setResult(null);

    const form = new FormData();
    form.append("file", file);

    try {
      const res = await fetch(`${API_URL}/api/extract`, {
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
        setError(data.detail ?? `Extraction failed (${res.status})`);
        return;
      }

      const data: ExtractResult = await res.json();
      setResult(data);
    } catch {
      setError("Could not reach the server. Is the backend running?");
    } finally {
      setLoading(false);
    }
  }

  async function handleDownload() {
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
  }

  function handleReset() {
    setFile(null);
    setResult(null);
    setError(null);
  }

  return (
    <SidebarLayout>
      {/* ── Dashboard / Upload State ── */}
      {!result && (
        <div className="mx-auto max-w-3xl space-y-6 p-4 sm:p-6 lg:p-8">
          {/* Upload zone */}
          <UploadZone file={file} onFile={setFile} disabled={loading} />

          {/* Extract button */}
          {file && (
            <div className="flex justify-center">
              <button
                onClick={handleExtract}
                disabled={loading}
                className="flex h-11 items-center gap-2 rounded-xl bg-violet-700 px-8 text-sm font-semibold text-white shadow-md shadow-violet-200 transition-all hover:bg-violet-800 disabled:opacity-60"
              >
                {loading ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    Extracting…
                  </>
                ) : (
                  <>
                    <FileSpreadsheet className="h-4 w-4" />
                    Run Extraction
                  </>
                )}
              </button>
            </div>
          )}

          {/* Error */}
          {error && (
            <div className="flex items-start gap-3 rounded-xl border border-red-200 dark:border-red-900/50 bg-red-50 dark:bg-red-950/30 px-4 py-3.5 text-sm text-red-700 dark:text-red-400">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>{error}</span>
            </div>
          )}

          {/* Bottom cards */}
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-5 shadow-sm">
              <div className="mb-3 flex items-center gap-2.5">
                <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-violet-50 dark:bg-violet-950/50">
                  <History className="h-4 w-4 text-violet-600 dark:text-violet-400" />
                </div>
                <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-200">Recent History</h3>
              </div>
              <p className="text-xs text-slate-500 dark:text-slate-400 leading-relaxed">
                View your previously extracted SPIR files and download past results.
              </p>
              <button className="mt-4 flex items-center gap-1.5 text-xs font-semibold text-violet-700 hover:text-violet-800 transition-colors">
                View History
                <ArrowUpRight className="h-3.5 w-3.5" />
              </button>
            </div>

            <div className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-5 shadow-sm">
              <div className="mb-3 flex items-center gap-2.5">
                <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-emerald-50 dark:bg-emerald-950/50">
                  <BookOpen className="h-4 w-4 text-emerald-600 dark:text-emerald-400" />
                </div>
                <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-200">System Guide</h3>
              </div>
              <p className="text-xs text-slate-500 dark:text-slate-400 leading-relaxed">
                Learn how to use SPIR Tool — formats, column mapping, error codes.
              </p>
              <button className="mt-4 flex items-center gap-1.5 text-xs font-semibold text-emerald-700 hover:text-emerald-800 transition-colors">
                Open Guide
                <ArrowUpRight className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Extraction Results State ── */}
      {result && (
        <div className="mx-auto max-w-6xl space-y-6 p-4 sm:p-6 lg:p-8">
          {/* Header bar */}
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <div className="flex items-center gap-2.5">
                <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-100 dark:bg-emerald-950 px-3 py-1 text-xs font-bold text-emerald-700 dark:text-emerald-400 border border-emerald-200 dark:border-emerald-800">
                  <CheckCircle2 className="h-3.5 w-3.5" />
                  Extraction Complete
                </span>
              </div>
              <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
                Successfully processed{" "}
                <span className="font-semibold text-slate-700 dark:text-slate-300">{file?.name}</span>
              </p>
            </div>
            <div className="flex items-center gap-2">
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
            </div>
          </div>

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
