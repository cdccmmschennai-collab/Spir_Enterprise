"use client";

import type { ReactNode } from "react";
import { AlertCircle, CheckCircle2, FileSpreadsheet, Loader2, RefreshCw, X } from "lucide-react";
import { cn, formatBytes } from "@/lib/utils";

// One presentation for every upload path. The page maps its own state onto a
// stage; this component never knows (or says) how the bytes travelled.
export type UploadStage = "uploading" | "processing" | "completed" | "failed" | "cancelled";

export interface UploadProgressCardProps {
  // "cancelled" is not rendered — the page returns to the select-file state.
  stage: Exclude<UploadStage, "cancelled">;
  filename: string;
  // Bytes; omit when unknown (e.g. restored after a reload).
  size?: number | null;
  // Real transfer progress while uploading. Without it the bar is indeterminate.
  loaded?: number;
  total?: number;
  // Failed: the error text.
  message?: string;
  onCancel?: () => void;
  cancelLabel?: string;
  // Failed: retry with the same file (only when the file is still selected).
  onRetry?: () => void;
  // Failed: back to file selection. Completed: extra actions (download, new file).
  onReset?: () => void;
  actions?: ReactNode;
}

const STATUS_TEXT: Record<UploadProgressCardProps["stage"], string> = {
  uploading: "Uploading your file...",
  processing: "Processing your file...",
  completed: "Extraction complete",
  failed: "Processing failed",
};

export function UploadProgressCard({
  stage,
  filename,
  size,
  loaded,
  total,
  message,
  onCancel,
  cancelLabel = "Cancel",
  onRetry,
  onReset,
  actions,
}: UploadProgressCardProps) {
  const active = stage === "uploading" || stage === "processing";
  const hasBytes = stage === "uploading" && typeof loaded === "number" && typeof total === "number" && total > 0;
  const pct = hasBytes ? Math.min(100, Math.round((loaded! / total!) * 100)) : null;
  const horizontal = stage === "completed";

  const icon =
    stage === "completed" ? (
      <CheckCircle2 className="h-8 w-8 text-emerald-600 dark:text-emerald-400" />
    ) : stage === "failed" ? (
      <AlertCircle className="h-8 w-8 text-red-600 dark:text-red-400" />
    ) : (
      <FileSpreadsheet className="h-8 w-8 text-violet-600 dark:text-violet-400" />
    );

  return (
    <section
      aria-live="polite"
      aria-busy={active}
      className={cn(
        "animate-fade-in rounded-2xl border bg-white shadow-sm dark:bg-slate-800",
        stage === "completed"
          ? "border-emerald-200 dark:border-emerald-800/50"
          : stage === "failed"
          ? "border-red-200 dark:border-red-900/50"
          : "border-violet-200 dark:border-violet-800/60",
        horizontal ? "p-5" : "px-6 py-8 sm:px-10"
      )}
    >
      <div
        className={cn(
          "flex gap-4",
          horizontal ? "flex-col sm:flex-row sm:items-center" : "flex-col items-center text-center"
        )}
      >
        {/* File identity */}
        <div
          className={cn(
            "flex h-16 w-16 shrink-0 items-center justify-center rounded-2xl",
            stage === "completed"
              ? "bg-emerald-50 dark:bg-emerald-950/50"
              : stage === "failed"
              ? "bg-red-50 dark:bg-red-950/50"
              : "bg-violet-100 dark:bg-violet-900/50"
          )}
        >
          {icon}
        </div>

        <div className={cn("min-w-0", horizontal ? "flex-1" : "w-full max-w-md")}>
          {filename && (
            <p className="truncate text-sm font-semibold text-slate-800 dark:text-slate-200" title={filename}>
              {filename}
            </p>
          )}
          {typeof size === "number" && (
            <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">{formatBytes(size)}</p>
          )}

          {/* Status line — the single spinner lives here */}
          <div
            className={cn(
              "flex items-center gap-2",
              horizontal ? "mt-1.5" : "mt-4 justify-center",
              stage === "completed"
                ? "text-emerald-700 dark:text-emerald-400"
                : stage === "failed"
                ? "text-red-700 dark:text-red-400"
                : "text-slate-800 dark:text-slate-100"
            )}
          >
            {active && <Loader2 className="h-4 w-4 shrink-0 animate-spin text-violet-600 dark:text-violet-400" />}
            <p className="text-base font-semibold">{STATUS_TEXT[stage]}</p>
          </div>

          {/* Progress bar */}
          {active && (
            <div className="mt-4">
              <div
                role="progressbar"
                aria-label={STATUS_TEXT[stage]}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={pct ?? undefined}
                aria-valuetext={pct === null ? "In progress" : `${pct}%`}
                className="h-2 w-full overflow-hidden rounded-full bg-slate-100 dark:bg-slate-700"
              >
                {pct === null ? (
                  <div className="h-full w-2/5 rounded-full bg-violet-500 animate-progress-indeterminate motion-reduce:animate-pulse" />
                ) : (
                  <div
                    className="h-full rounded-full bg-violet-600 transition-[width] duration-300 ease-out"
                    style={{ width: `${pct}%` }}
                  />
                )}
              </div>
              {hasBytes && (
                <div className="mt-2 flex items-center justify-between text-xs tabular-nums text-slate-500 dark:text-slate-400">
                  <span>
                    {formatBytes(loaded!)} of {formatBytes(total!)}
                  </span>
                  <span className="font-semibold text-slate-700 dark:text-slate-300">{pct}%</span>
                </div>
              )}
            </div>
          )}

          {stage === "failed" && message && (
            <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">{message}</p>
          )}

          {/* Secondary actions */}
          {active && onCancel && (
            <div className="mt-4 flex justify-center">
              <button
                type="button"
                onClick={onCancel}
                className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-700 dark:text-slate-400 dark:hover:bg-slate-700 dark:hover:text-slate-200"
              >
                <X className="h-3.5 w-3.5" /> {cancelLabel}
              </button>
            </div>
          )}

          {stage === "failed" && (onRetry || onReset) && (
            <div className="mt-5 flex flex-wrap justify-center gap-2">
              {onRetry && (
                <button
                  type="button"
                  onClick={onRetry}
                  className="flex h-9 items-center gap-2 rounded-xl bg-violet-700 px-4 text-sm font-semibold text-white shadow-md shadow-violet-200 transition-colors hover:bg-violet-800 dark:shadow-none"
                >
                  <RefreshCw className="h-3.5 w-3.5" />
                  Try again
                </button>
              )}
              {onReset && (
                <button
                  type="button"
                  onClick={onReset}
                  className="flex h-9 items-center gap-2 rounded-xl border border-slate-200 bg-white px-4 text-sm font-medium text-slate-600 shadow-sm transition-colors hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
                >
                  Choose another file
                </button>
              )}
            </div>
          )}
        </div>

        {stage === "completed" && actions && (
          <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>
        )}
      </div>
    </section>
  );
}
