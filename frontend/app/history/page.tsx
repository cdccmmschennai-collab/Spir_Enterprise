"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { History, Loader2 } from "lucide-react";
import { SidebarLayout } from "@/components/sidebar";
import { authHeaders } from "@/lib/auth";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

interface HistoryItem {
  id: string;
  filename: string;
  spir_no: string | null;
  tag_count: number;
  spare_count: number;
  created_at: string;
}

export default function HistoryPage() {
  const router = useRouter();
  const [items, setItems] = useState<HistoryItem[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function load() {
      try {
        const res = await fetch(`${API_URL}/api/history`, {
          headers: { ...authHeaders() },
          cache: "no-store",
        });
        if (res.status === 401) {
          router.push("/login");
          return;
        }
        if (!res.ok) return;
        const data: HistoryItem[] = await res.json();
        setItems(data);
      } finally {
        setLoading(false);
      }
    }
    load();

    const onFocus = () => load();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [router]);

  return (
    <SidebarLayout>
      <div className="mx-auto max-w-5xl space-y-6 p-4 sm:p-6 lg:p-8">
        <div>
          <h1 className="text-xl font-bold text-slate-900 dark:text-slate-100">Extraction History</h1>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
            Previously extracted SPIR files and their results
          </p>
        </div>

        {loading && (
          <div className="flex items-center justify-center py-16">
            <Loader2 className="h-6 w-6 animate-spin text-violet-500" />
          </div>
        )}

        {!loading && items.length === 0 && (
          <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 py-16 text-center">
            <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-slate-100 dark:bg-slate-700">
              <History className="h-7 w-7 text-slate-400 dark:text-slate-500" />
            </div>
            <h3 className="mt-4 text-sm font-semibold text-slate-700 dark:text-slate-300">No history yet</h3>
            <p className="mt-1 text-xs text-slate-400 dark:text-slate-500 max-w-xs">
              Your extraction history will appear here after you process your first SPIR file.
            </p>
          </div>
        )}

        {!loading && items.length > 0 && (
          <div className="overflow-hidden rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-100 dark:border-slate-700 bg-slate-50 dark:bg-slate-900">
                  {["Filename", "SPIR No.", "Tags", "Spares", "Date"].map((h, i) => (
                    <th
                      key={h}
                      className={`px-4 py-3 text-xs font-semibold uppercase tracking-wider text-slate-500 ${i >= 2 && i <= 3 ? "text-right" : "text-left"}`}
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-slate-700">
                {items.map((item) => (
                  <tr key={item.id} className="hover:bg-slate-50 dark:hover:bg-slate-700/50 transition-colors">
                    <td className="px-4 py-3 max-w-xs truncate font-medium text-slate-800 dark:text-slate-200">
                      {item.filename}
                    </td>
                    <td className="px-4 py-3 text-slate-600 dark:text-slate-400">{item.spir_no ?? "—"}</td>
                    <td className="px-4 py-3 text-right tabular-nums text-slate-600 dark:text-slate-400">{item.tag_count}</td>
                    <td className="px-4 py-3 text-right tabular-nums text-slate-600 dark:text-slate-400">{item.spare_count}</td>
                    <td className="px-4 py-3 text-xs text-slate-500 dark:text-slate-500 whitespace-nowrap">
                      {new Date(item.created_at).toLocaleString("en-IN", {
                        timeZone: "Asia/Kolkata",
                        day: "2-digit",
                        month: "short",
                        year: "numeric",
                        hour: "2-digit",
                        minute: "2-digit",
                        hour12: true,
                      })}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </SidebarLayout>
  );
}
