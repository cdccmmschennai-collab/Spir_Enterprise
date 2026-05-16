"use client";

import { useRouter } from "next/navigation";
import { Upload, Cpu, ClipboardList, Layers, Download, ArrowRight, BookOpen } from "lucide-react";
import { SidebarLayout } from "@/components/sidebar";

const steps = [
  {
    number: 1,
    icon: Upload,
    title: "Upload File",
    description:
      "Upload your split file (XLSX, XLS, CSV supported). Single or multiple files can be extracted.",
    action: { label: "Go to Extraction", href: "/extraction" },
  },
  {
    number: 2,
    icon: Cpu,
    title: "Run Extraction",
    description:
      "Click 'Run Extraction' on the Extraction page. The engine auto-detects sheet types (Main, Continuation, Annexure) and extracts structured BOM data.",
    action: null,
  },
  {
    number: 3,
    icon: ClipboardList,
    title: "Review Results",
    description:
      "Inspect extracted tags, model numbers, serial numbers, and SPIR type. Verify the row count and confirm accuracy before exporting.",
    action: null,
  },
  {
    number: 4,
    icon: Layers,
    title: "Combine Files",
    description:
      "Select multiple extracted files from History and combine them into one consolidated output.",
    action: { label: "Go to History", href: "/history" },
  },
  {
    number: 5,
    icon: Download,
    title: "Download Output",
    description: "Export the final result as Excel (.xlsx).",
    action: null,
  },
];

export default function GuidePage() {
  const router = useRouter();

  return (
    <SidebarLayout>
      <div className="mx-auto max-w-2xl space-y-6 p-6 lg:p-10">
        {/* Header */}
        <div>
          <div className="flex items-center gap-2 mb-1">
            <BookOpen className="h-5 w-5 text-violet-600" />
            <h1 className="text-lg font-bold text-slate-900 dark:text-slate-100 tracking-tight">
              System Guide
            </h1>
          </div>
          <p className="text-sm text-slate-500 dark:text-slate-400">
            Five steps to complete your SPIR extraction workflow.
          </p>
        </div>

        {/* Steps */}
        <ol className="space-y-3">
          {steps.map((step) => {
            const Icon = step.icon;
            return (
              <li
                key={step.number}
                className="flex gap-4 rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-5 shadow-sm"
              >
                {/* Step number + icon */}
                <div className="flex flex-col items-center gap-1 shrink-0">
                  <span className="text-sm font-extrabold tabular-nums text-violet-600 dark:text-violet-400">
                    {step.number}
                  </span>
                  <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-slate-100 dark:bg-slate-700">
                    <Icon className="h-4 w-4 text-slate-600 dark:text-slate-300" />
                  </div>
                </div>

                {/* Content */}
                <div className="flex flex-1 flex-col gap-1 min-w-0">
                  <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-100">
                    {step.title}
                  </h3>
                  <p className="text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                    {step.description}
                  </p>
                  {step.action && (
                    <button
                      onClick={() => router.push(step.action!.href)}
                      className="mt-2 inline-flex w-fit items-center gap-1.5 rounded-lg bg-violet-600 hover:bg-violet-700 active:bg-violet-800 px-3 py-1.5 text-xs font-semibold text-white transition-colors"
                    >
                      {step.action.label}
                      <ArrowRight className="h-3 w-3" />
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ol>

      </div>
    </SidebarLayout>
  );
}
