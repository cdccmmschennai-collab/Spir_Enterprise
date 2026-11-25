"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { cn } from "@/lib/utils";
import { usePreviousPath } from "@/components/nav-history";

interface PageHeaderProps {
  title: string;
  description?: string;
  /** Show a Back button: returns to the previous page, or `backFallback` if there is none. */
  back?: boolean;
  backFallback?: string;
  actions?: React.ReactNode;
  className?: string;
}

function PageHeader({ title, description, back, backFallback = "/extraction", actions, className }: PageHeaderProps) {
  const router = useRouter();
  const previousPath = usePreviousPath();

  function goBack() {
    // A direct visit / reload has no in-app page to return to.
    if (previousPath && previousPath !== "/login") router.back();
    else router.push(backFallback);
  }

  return (
    <div className={cn("flex flex-wrap items-end justify-between gap-4", className)}>
      <div className="min-w-0">
        {back && (
          <button
            type="button"
            onClick={goBack}
            className="mb-3 inline-flex items-center gap-1.5 rounded-full py-1 pr-2 text-xs font-semibold text-slate-500 transition-colors hover:text-brand-700 dark:text-slate-400 dark:hover:text-brand-300"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            Back
          </button>
        )}
        <h1 className="text-2xl font-bold tracking-[-0.02em] text-slate-900 dark:text-white">{title}</h1>
        {description && <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">{description}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

export { PageHeader };
