"use client";

import { Table2 } from "lucide-react";
import { SidebarLayout } from "@/components/sidebar";

// Stub page — data tables feature coming soon
export default function DataTablesPage() {
  return (
    <SidebarLayout>
      <div className="mx-auto max-w-4xl space-y-6 p-4 sm:p-6 lg:p-8">
        <div>
          <h1 className="text-xl font-bold text-slate-900">Data Tables</h1>
          <p className="mt-1 text-sm text-slate-500">
            Browse and search your extracted spare parts data
          </p>
        </div>

        <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-slate-200 bg-white py-16 text-center">
          <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-slate-100">
            <Table2 className="h-7 w-7 text-slate-400" />
          </div>
          <h3 className="mt-4 text-sm font-semibold text-slate-700">Coming soon</h3>
          <p className="mt-1 text-xs text-slate-400 max-w-xs">
            Aggregate data tables across all extracted SPIR files will be available here.
          </p>
        </div>
      </div>
    </SidebarLayout>
  );
}
