"use client";

import { useRouter } from "next/navigation";
import {
  Upload,
  Cpu,
  ClipboardList,
  Layers,
  Download,
  ArrowRight,
  BookOpen,
  Package,
  ArrowLeft,
} from "lucide-react";
import { SidebarLayout } from "@/components/sidebar";

// ── Sub-components ────────────────────────────────────────────────────────────

function SectionLabel({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-3">
      <div className="flex-1 h-px bg-slate-200 dark:bg-slate-700" />
      <span className="shrink-0 text-[10px] font-semibold uppercase tracking-widest text-slate-400 dark:text-slate-500">
        {label}
      </span>
      <div className="flex-1 h-px bg-slate-200 dark:bg-slate-700" />
    </div>
  );
}

function OrDivider() {
  return (
    <div className="flex items-center gap-3">
      <div className="flex-1 h-px bg-slate-200 dark:bg-slate-700" />
      <span className="shrink-0 text-[11px] font-bold uppercase tracking-wider text-slate-400 dark:text-slate-500">
        or
      </span>
      <div className="flex-1 h-px bg-slate-200 dark:bg-slate-700" />
    </div>
  );
}

interface StepItemProps {
  number: number;
  title: string;
  description: string;
  isLast: boolean;
  accent: "violet" | "emerald";
  action?: { label: string; href: string };
}

function StepItem({ number, title, description, isLast, accent, action }: StepItemProps) {
  const router = useRouter();

  const circleBg =
    accent === "violet"
      ? "bg-violet-600 dark:bg-violet-500"
      : "bg-emerald-600 dark:bg-emerald-500";

  const btnBg =
    accent === "violet"
      ? "bg-violet-600 hover:bg-violet-700 active:bg-violet-800"
      : "bg-emerald-600 hover:bg-emerald-700 active:bg-emerald-800";

  return (
    <div className="flex gap-3">
      {/* Timeline column */}
      <div className="flex flex-col items-center shrink-0">
        <div
          className={`flex h-6 w-6 items-center justify-center rounded-full text-white text-[10px] font-bold ${circleBg}`}
        >
          {number}
        </div>
        {!isLast && (
          <div className="w-px flex-1 min-h-[28px] bg-slate-200 dark:bg-slate-700 mt-1" />
        )}
      </div>

      {/* Content column */}
      <div className={`flex-1 min-w-0 ${isLast ? "pt-0" : "pb-5"}`}>
        <p className="text-sm font-semibold text-slate-800 dark:text-slate-100 leading-snug">
          {title}
        </p>
        <p className="mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
          {description}
        </p>
        {action && (
          <button
            onClick={() => router.push(action.href)}
            className={`mt-2.5 inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold text-white transition-colors ${btnBg}`}
          >
            {action.label}
            <ArrowRight className="h-3 w-3" />
          </button>
        )}
      </div>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function GuidePage() {
  const router = useRouter();

  return (
    <SidebarLayout>
      <div className="mx-auto max-w-2xl p-6 lg:p-10">

        {/* Header - back arrow inline with title */}
        <div className="flex items-center gap-2 mb-8">
          <button
            onClick={() => router.back()}
            aria-label="Go back"
            className="flex items-center justify-center rounded-lg p-1.5 text-slate-400 hover:text-violet-600 dark:hover:text-violet-400 hover:bg-violet-50 dark:hover:bg-violet-950/40 transition-colors"
          >
            <ArrowLeft className="h-4 w-4" />
          </button>
          <BookOpen className="h-5 w-5 text-violet-600" />
          <h1 className="text-lg font-bold text-slate-900 dark:text-slate-100 tracking-tight">
            System Guide
          </h1>
        </div>

        <div className="space-y-5">

          {/* ── Extraction ── */}
          <SectionLabel label="Extraction" />

          {/* Path A — Single File */}
          <div className="rounded-xl border border-slate-200 dark:border-slate-700 border-l-4 border-l-violet-500 dark:border-l-violet-400 bg-white dark:bg-slate-800 px-5 pt-5 pb-4 shadow-sm">
            <span className="inline-block mb-4 rounded-full bg-violet-50 dark:bg-violet-950/60 px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider text-violet-700 dark:text-violet-300">
              Single File
            </span>
            <div>
              <StepItem
                number={1}
                title="Upload File"
                description="Upload one SPIR file (XLSX, XLS, CSV supported)."
                isLast={false}
                accent="violet"
                action={{ label: "Go to Extraction", href: "/extraction" }}
              />
              <StepItem
                number={2}
                title="Run Extraction"
                description="The engine auto-detects sheet types (Main, Continuation, Annexure) and extracts structured BOM data."
                isLast={false}
                accent="violet"
              />
              <StepItem
                number={3}
                title="Review Results"
                description="Inspect extracted tags, model numbers, serial numbers, and SPIR type. Verify accuracy before proceeding."
                isLast={true}
                accent="violet"
              />
            </div>
          </div>

          <OrDivider />

          {/* Path B — Batch Processing */}
          <div className="rounded-xl border border-slate-200 dark:border-slate-700 border-l-4 border-l-emerald-500 dark:border-l-emerald-400 bg-white dark:bg-slate-800 px-5 pt-5 pb-4 shadow-sm">
            <span className="inline-block mb-4 rounded-full bg-emerald-50 dark:bg-emerald-950/60 px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider text-emerald-700 dark:text-emerald-300">
              Batch Processing
            </span>
            <div>
              <StepItem
                number={1}
                title="Upload Multiple Files"
                description="Select several SPIR files at once (XLSX, XLS, CSV supported) and submit them as a batch."
                isLast={false}
                accent="emerald"
                action={{ label: "Go to Batch", href: "/batch" }}
              />
              <StepItem
                number={2}
                title="Auto Extract & Merge"
                description="Files are extracted sequentially. Successful outputs are automatically merged - download the consolidated result directly from the Batch page."
                isLast={true}
                accent="emerald"
              />
            </div>
          </div>

          {/* ── Output ── */}
          <SectionLabel label="Output" />

          {/* Combine Files */}
          <div className="flex gap-4 rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-5 shadow-sm">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-slate-100 dark:bg-slate-700">
              <Layers className="h-4 w-4 text-slate-600 dark:text-slate-300" />
            </div>
            <div className="flex-1 min-w-0">
              <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-100">
                Combine Files
              </h3>
              <p className="mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                Merge extracted outputs into one file - two ways:
              </p>
              <ul className="mt-2 space-y-1.5">
                <li className="flex items-start gap-2 text-xs text-slate-500 dark:text-slate-400">
                  <span className="mt-1.5 shrink-0 h-1.5 w-1.5 rounded-full bg-violet-400 dark:bg-violet-500" />
                  <span>
                    <span className="font-semibold text-slate-700 dark:text-slate-300">
                      From History
                    </span>{" "}
                    - selectively pick any previously extracted files and combine them.
                  </span>
                </li>
                <li className="flex items-start gap-2 text-xs text-slate-500 dark:text-slate-400">
                  <span className="mt-1.5 shrink-0 h-1.5 w-1.5 rounded-full bg-emerald-400 dark:bg-emerald-500" />
                  <span>
                    <span className="font-semibold text-slate-700 dark:text-slate-300">
                      After Batch
                    </span>{" "}
                    - auto-merged immediately when the batch run completes.
                  </span>
                </li>
              </ul>
              <button
                onClick={() => router.push("/history")}
                className="mt-3 inline-flex items-center gap-1.5 rounded-lg bg-violet-600 hover:bg-violet-700 active:bg-violet-800 px-3 py-1.5 text-xs font-semibold text-white transition-colors"
              >
                Go to History <ArrowRight className="h-3 w-3" />
              </button>
            </div>
          </div>

          {/* Download Output */}
          <div className="flex gap-4 rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-5 shadow-sm">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-slate-100 dark:bg-slate-700">
              <Download className="h-4 w-4 text-slate-600 dark:text-slate-300" />
            </div>
            <div className="flex-1 min-w-0">
              <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-100">
                Download Output
              </h3>
              <p className="mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                Export the final merged result as a single Excel (.xlsx) file.
              </p>
            </div>
          </div>

        </div>
      </div>
    </SidebarLayout>
  );
}
