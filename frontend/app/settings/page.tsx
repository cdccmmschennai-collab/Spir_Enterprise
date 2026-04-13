"use client";

import { Settings } from "lucide-react";
import { SidebarLayout } from "@/components/sidebar";

// Stub page — settings to be expanded
export default function SettingsPage() {
  return (
    <SidebarLayout>
      <div className="mx-auto max-w-4xl space-y-6 p-4 sm:p-6 lg:p-8">
        <div>
          <h1 className="text-xl font-bold text-slate-900">Settings</h1>
          <p className="mt-1 text-sm text-slate-500">
            Manage your account and workspace preferences
          </p>
        </div>

        <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-slate-200 bg-white py-16 text-center">
          <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-slate-100">
            <Settings className="h-7 w-7 text-slate-400" />
          </div>
          <h3 className="mt-4 text-sm font-semibold text-slate-700">Settings panel</h3>
          <p className="mt-1 text-xs text-slate-400 max-w-xs">
            Account settings, API keys, and workspace configuration will appear here.
          </p>
        </div>
      </div>
    </SidebarLayout>
  );
}
