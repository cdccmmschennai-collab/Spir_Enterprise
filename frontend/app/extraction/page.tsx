"use client";

import { useCallback, useRef, useState } from "react";
import {
  Upload,
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
  ChevronDown,
  ChevronUp,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { SidebarLayout } from "@/components/sidebar";
import { authHeaders } from "@/lib/auth";
import { cn } from "@/lib/utils";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

const ACCEPTED = ".xlsx,.xlsm,.xls";

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

// ─── Upload Zone ─────────────────────────────────────────────────────────────

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

  if (file) {
    return (
      <div className="flex items-center gap-4 rounded-xl border border-blue-200 bg-blue-50 px-5 py-4">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-blue-100">
          <FileSpreadsheet className="h-5 w-5 text-blue-600" />
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-slate-900">
            {file.name}
          </p>
          <p className="text-xs text-slate-500">{formatBytes(file.size)}</p>
        </div>
        {!disabled && (
          <button
            onClick={() => onFile(null)}
            className="rounded-md p-1 text-slate-400 hover:bg-blue-100 hover:text-slate-600"
          >
            <X className="h-4 w-4" />
          </button>
        )}
      </div>
    );
  }

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        if (!disabled) setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={handleDrop}
      onClick={() => !disabled && inputRef.current?.click()}
      className={cn(
        "flex cursor-pointer flex-col items-center justify-center gap-4 rounded-xl border-2 border-dashed px-6 py-12 transition-all duration-200",
        dragging
          ? "border-blue-400 bg-blue-50"
          : "border-slate-200 bg-slate-50 hover:border-blue-300 hover:bg-blue-50/50",
        disabled && "cursor-not-allowed opacity-60"
      )}
    >
      <div
        className={cn(
          "flex h-14 w-14 items-center justify-center rounded-2xl transition-colors",
          dragging ? "bg-blue-100" : "bg-white shadow-sm"
        )}
      >
        <Upload
          className={cn(
            "h-6 w-6 transition-colors",
            dragging ? "text-blue-600" : "text-slate-400"
          )}
        />
      </div>
      <div className="text-center">
        <p className="text-sm font-medium text-slate-700">
          {dragging ? "Drop to upload" : "Drag & drop your SPIR file"}
        </p>
        <p className="mt-1 text-xs text-slate-400">
          or{" "}
          <span className="font-medium text-blue-600 hover:underline">
            click to browse
          </span>{" "}
          · .xlsx, .xlsm, .xls
        </p>
      </div>
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

// ─── Stats Card ───────────────────────────────────────────────────────────────

interface StatCardProps {
  icon: React.ElementType;
  label: string;
  value: string | number;
  iconClass?: string;
  bgClass?: string;
}

function StatCard({ icon: Icon, label, value, iconClass = "text-blue-600", bgClass = "bg-blue-50" }: StatCardProps) {
  return (
    <div className="flex items-center gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
      <div className={cn("flex h-9 w-9 shrink-0 items-center justify-center rounded-lg", bgClass)}>
        <Icon className={cn("h-4 w-4", iconClass)} />
      </div>
      <div className="min-w-0">
        <p className="text-xs text-slate-500">{label}</p>
        <p className="text-sm font-semibold text-slate-900 truncate">{value}</p>
      </div>
    </div>
  );
}

// ─── Preview Table ─────────────────────────────────────────────────────────────

interface PreviewTableProps {
  cols: string[];
  rows: (string | number | null)[][];
}

function PreviewTable({ cols, rows }: PreviewTableProps) {
  const [expanded, setExpanded] = useState(false);

  const displayRows = expanded ? rows : rows.slice(0, 8);

  return (
    <div className="space-y-2">
      <div className="overflow-x-auto rounded-xl border border-slate-200 shadow-sm">
        <table className="min-w-full text-xs">
          <thead className="sticky top-0 z-10">
            <tr className="bg-slate-50">
              {cols.map((col, i) => (
                <th
                  key={i}
                  className="whitespace-nowrap border-b border-slate-200 px-3 py-2.5 text-left font-semibold text-slate-600 first:pl-4 last:pr-4"
                >
                  {col}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 bg-white">
            {displayRows.length === 0 ? (
              <tr>
                <td
                  colSpan={cols.length}
                  className="px-4 py-8 text-center text-slate-400"
                >
                  No data rows
                </td>
              </tr>
            ) : (
              displayRows.map((row, ri) => (
                <tr
                  key={ri}
                  className="transition-colors hover:bg-slate-50"
                >
                  {cols.map((_, ci) => {
                    const val = row[ci];
                    const isDuplicate = val === "DUPLICATE";
                    return (
                      <td
                        key={ci}
                        className="max-w-[200px] truncate whitespace-nowrap px-3 py-2 text-slate-700 first:pl-4 last:pr-4"
                        title={val != null ? String(val) : ""}
                      >
                        {isDuplicate ? (
                          <Badge variant="destructive" className="text-[10px]">
                            DUPLICATE
                          </Badge>
                        ) : val === 0 ? (
                          <span className="text-slate-300">—</span>
                        ) : val != null ? (
                          String(val)
                        ) : (
                          <span className="text-slate-200">—</span>
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {rows.length > 8 && (
        <button
          onClick={() => setExpanded((p) => !p)}
          className="flex w-full items-center justify-center gap-1.5 py-2 text-xs font-medium text-blue-600 hover:text-blue-700"
        >
          {expanded ? (
            <>
              <ChevronUp className="h-3.5 w-3.5" /> Show fewer rows
            </>
          ) : (
            <>
              <ChevronDown className="h-3.5 w-3.5" /> Show all {rows.length} preview rows
            </>
          )}
        </button>
      )}
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
        headers: authHeaders(), // Authorization header only — no Content-Type (multipart)
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
      <div className="mx-auto max-w-6xl space-y-6 p-4 sm:p-6 lg:p-8">
        {/* Page header */}
        <div className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-xl font-bold text-slate-900 sm:text-2xl">
              SPIR Extraction
            </h1>
            <p className="mt-1 text-sm text-slate-500">
              Upload a SPIR Excel file to extract and standardize spare parts data
            </p>
          </div>
          {result && (
            <Button variant="outline" size="sm" onClick={handleReset}>
              <X className="h-3.5 w-3.5" />
              New file
            </Button>
          )}
        </div>

        {/* Upload + extract card */}
        <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm space-y-4 sm:p-6">
          <div className="flex items-center gap-2">
            <FileSpreadsheet className="h-4 w-4 text-blue-600" />
            <h2 className="text-sm font-semibold text-slate-800">
              Upload SPIR File
            </h2>
          </div>

          <UploadZone
            file={file}
            onFile={setFile}
            disabled={loading}
          />

          <div className="flex flex-col gap-3 pt-1 sm:flex-row sm:items-center sm:justify-between">
            <p className="text-xs text-slate-400">
              Supported: .xlsx, .xlsm, .xls · Max 2 GB
            </p>
            <Button
              onClick={handleExtract}
              disabled={!file || loading}
              size="lg"
              className="w-full sm:w-auto sm:min-w-[140px]"
            >
              {loading ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Extracting…
                </>
              ) : (
                <>
                  <Upload className="h-4 w-4" />
                  Extract
                </>
              )}
            </Button>
          </div>
        </div>

        {/* Error */}
        {error && (
          <div className="flex items-start gap-3 rounded-xl border border-red-200 bg-red-50 px-4 py-3.5 text-sm text-red-700 animate-fade-in">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        {/* Results */}
        {result && (
          <div className="space-y-6 animate-fade-in">
            {/* Success banner */}
            <div className="flex flex-wrap items-center gap-3 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3.5 text-sm text-emerald-700">
              <CheckCircle2 className="h-4 w-4 shrink-0" />
              <span>
                Extraction complete —{" "}
                <strong>{result.total_rows}</strong> rows extracted from{" "}
                <strong>{file?.name}</strong>
              </span>
            </div>

            {/* Stats grid */}
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <StatCard
                icon={Hash}
                label="Total Rows"
                value={result.total_rows.toLocaleString()}
                iconClass="text-blue-600"
                bgClass="bg-blue-50"
              />
              <StatCard
                icon={Tag}
                label="Tags"
                value={result.total_tags.toLocaleString()}
                iconClass="text-violet-600"
                bgClass="bg-violet-50"
              />
              <StatCard
                icon={Layers}
                label="Spare Items"
                value={result.spare_items.toLocaleString()}
                iconClass="text-emerald-600"
                bgClass="bg-emerald-50"
              />
              <StatCard
                icon={AlertTriangle}
                label="Duplicates"
                value={result.dup1_count.toLocaleString()}
                iconClass={result.dup1_count > 0 ? "text-amber-600" : "text-slate-400"}
                bgClass={result.dup1_count > 0 ? "bg-amber-50" : "bg-slate-50"}
              />
            </div>

            {/* Metadata */}
            <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm sm:p-5">
              <h2 className="mb-4 text-sm font-semibold text-slate-800">
                SPIR Metadata
              </h2>
              <div className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3 lg:grid-cols-4 text-sm">
                {[
                  { label: "SPIR Number", value: result.spir_no },
                  { label: "Equipment", value: result.equipment },
                  { label: "Manufacturer", value: result.manufacturer },
                  { label: "Supplier", value: result.supplier },
                  { label: "SPIR Type", value: result.spir_type },
                  { label: "Format", value: result.format },
                  { label: "Eqpt Qty", value: result.eqpt_qty || "—" },
                  { label: "Annexures", value: result.annexure_count || "—" },
                ].map(({ label, value }) =>
                  value ? (
                    <div key={label}>
                      <p className="text-[10px] font-medium uppercase tracking-wide text-slate-400">
                        {label}
                      </p>
                      <p className="mt-0.5 truncate font-medium text-slate-800">
                        {String(value)}
                      </p>
                    </div>
                  ) : null
                )}
              </div>
            </div>

            {/* Preview table */}
            <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm sm:p-5">
              <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <h2 className="text-sm font-semibold text-slate-800">
                    Preview
                  </h2>
                  <p className="text-xs text-slate-400">
                    Showing first {Math.min(result.preview_rows.length, 8)} of{" "}
                    {result.total_rows} rows · {result.preview_cols.length} columns
                  </p>
                </div>
                <Button
                  onClick={handleDownload}
                  disabled={downloading}
                  variant="default"
                  size="sm"
                  className="w-full min-h-[44px] sm:w-auto sm:min-h-0"
                >
                  {downloading ? (
                    <>
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      Downloading…
                    </>
                  ) : (
                    <>
                      <Download className="h-3.5 w-3.5" />
                      Download Excel
                    </>
                  )}
                </Button>
              </div>

              <PreviewTable
                cols={result.preview_cols}
                rows={result.preview_rows}
              />
            </div>
          </div>
        )}
      </div>
    </SidebarLayout>
  );
}
