"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { History, Loader2, Download, CheckSquare, Square, FileX, Trash2 } from "lucide-react";
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
  file_id?: string | null;
  total_rows?: number | null;
}

export default function HistoryPage() {
  const router = useRouter();
  const [items, setItems] = useState<HistoryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [combining, setCombining] = useState(false);
  const [combineError, setCombineError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);

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
        setSelected(new Set()); // reset selection on reload
      } finally {
        setLoading(false);
      }
    }
    load();

    const onFocus = () => load();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [router]);

  function toggleRow(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
    setCombineError(null);
  }

  function toggleAll() {
    if (selected.size === items.length) {
      setSelected(new Set());
    } else {
      setSelected(new Set(items.map((i) => i.id)));
    }
    setCombineError(null);
  }

  async function handleCombine() {
    if (selected.size === 0) return;
    setCombining(true);
    setCombineError(null);
    try {
      const res = await fetch(`${API_URL}/api/combine`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...authHeaders(),
        },
        body: JSON.stringify({ history_ids: Array.from(selected) }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail ?? "Combine failed");
      }

      // Trigger browser download
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "COMBINED_Extraction.xlsx";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setCombineError(msg);
    } finally {
      setCombining(false);
    }
  }

  async function handleDelete() {
    if (selected.size === 0) return;
    setDeleting(true);
    setDeleteError(null);
    setShowDeleteConfirm(false);
    try {
      const res = await fetch(`${API_URL}/api/history`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ history_ids: Array.from(selected) }),
      });
      if (res.status === 401) {
        router.push("/login");
        return;
      }
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail ?? "Delete failed");
      }
      const refreshed = await fetch(`${API_URL}/api/history`, {
        headers: { ...authHeaders() },
        cache: "no-store",
      });
      if (refreshed.ok) {
        const data: HistoryItem[] = await refreshed.json();
        setItems(data);
      }
      setSelected(new Set());
    } catch (e: unknown) {
      setDeleteError(e instanceof Error ? e.message : String(e));
    } finally {
      setDeleting(false);
    }
  }

  const allSelected = items.length > 0 && selected.size === items.length;
  const someSelected = selected.size > 0 && selected.size < items.length;

  return (
    <SidebarLayout>
      <div className="mx-auto max-w-5xl space-y-6 p-4 sm:p-6 lg:p-8">

        {/* Header */}
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div>
            <h1 className="text-xl font-bold text-slate-900 dark:text-slate-100">Extraction History</h1>
            <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
              Select files and combine them into one merged Excel
            </p>
          </div>

          {selected.size > 0 && (
            <div className="flex items-center gap-2 flex-wrap">
              <button
                onClick={handleCombine}
                disabled={combining || deleting}
                className="flex items-center gap-2 rounded-xl bg-violet-600 hover:bg-violet-700 disabled:opacity-60 disabled:cursor-not-allowed px-4 py-2 text-sm font-semibold text-white transition-colors shadow-sm"
              >
                {combining ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Download className="h-4 w-4" />
                )}
                {combining
                  ? "Building combined Excel…"
                  : `Combine ${selected.size} file${selected.size > 1 ? "s" : ""} → Download Excel`}
              </button>

              <button
                onClick={() => setShowDeleteConfirm(true)}
                disabled={deleting || combining}
                className="flex items-center gap-2 rounded-xl bg-red-600 hover:bg-red-700 disabled:opacity-60 disabled:cursor-not-allowed px-4 py-2 text-sm font-semibold text-white transition-colors shadow-sm"
              >
                {deleting ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Trash2 className="h-4 w-4" />
                )}
                {deleting ? "Deleting…" : `Delete ${selected.size} selected`}
              </button>
            </div>
          )}
        </div>

        {/* Combine error */}
        {combineError && (
          <div className="flex items-start gap-3 rounded-xl border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/20 px-4 py-3 text-sm text-red-700 dark:text-red-300">
            <FileX className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{combineError}</span>
          </div>
        )}

        {/* Delete error */}
        {deleteError && (
          <div className="flex items-start gap-3 rounded-xl border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/20 px-4 py-3 text-sm text-red-700 dark:text-red-300">
            <FileX className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{deleteError}</span>
          </div>
        )}

        {/* Delete confirmation dialog */}
        {showDeleteConfirm && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
            <div className="rounded-2xl bg-white dark:bg-slate-800 p-6 shadow-xl max-w-sm w-full mx-4 space-y-4">
              <h2 className="text-base font-semibold text-slate-900 dark:text-slate-100">Confirm Delete</h2>
              <p className="text-sm text-slate-600 dark:text-slate-400">
                Delete selected extraction history and associated stored row data?
              </p>
              <div className="flex gap-3 justify-end">
                <button
                  onClick={() => setShowDeleteConfirm(false)}
                  className="px-4 py-2 text-sm rounded-xl border border-slate-200 dark:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-300 transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={handleDelete}
                  className="px-4 py-2 text-sm rounded-xl bg-red-600 hover:bg-red-700 text-white font-semibold transition-colors"
                >
                  Delete
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Loading spinner */}
        {loading && (
          <div className="flex items-center justify-center py-16">
            <Loader2 className="h-6 w-6 animate-spin text-violet-500" />
          </div>
        )}

        {/* Empty state */}
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

        {/* History table */}
        {!loading && items.length > 0 && (
          <div className="overflow-hidden rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-100 dark:border-slate-700 bg-slate-50 dark:bg-slate-900">
                  {/* Select-all checkbox */}
                  <th className="w-10 px-3 py-3">
                    <button
                      onClick={toggleAll}
                      className="flex items-center justify-center text-slate-400 hover:text-violet-600 dark:hover:text-violet-400 transition-colors"
                      title={allSelected ? "Deselect all" : "Select all"}
                    >
                      {allSelected ? (
                        <CheckSquare className="h-4 w-4 text-violet-600 dark:text-violet-400" />
                      ) : someSelected ? (
                        <CheckSquare className="h-4 w-4 text-violet-400" />
                      ) : (
                        <Square className="h-4 w-4" />
                      )}
                    </button>
                  </th>
                  {["Filename", "SPIR No.", "Rows", "Tags", "Spares", "Date"].map((h, i) => (
                    <th
                      key={h}
                      className={`px-4 py-3 text-xs font-semibold uppercase tracking-wider text-slate-500 ${
                        i >= 2 && i <= 4 ? "text-right" : "text-left"
                      }`}
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-slate-700">
                {items.map((item) => {
                  const isChecked = selected.has(item.id);
                  return (
                    <tr
                      key={item.id}
                      onClick={() => toggleRow(item.id)}
                      className={`cursor-pointer transition-colors ${
                        isChecked
                          ? "bg-violet-50 dark:bg-violet-900/20"
                          : "hover:bg-slate-50 dark:hover:bg-slate-700/50"
                      }`}
                    >
                      <td className="w-10 px-3 py-3">
                        <div className="flex items-center justify-center">
                          {isChecked ? (
                            <CheckSquare className="h-4 w-4 text-violet-600 dark:text-violet-400" />
                          ) : (
                            <Square className="h-4 w-4 text-slate-300 dark:text-slate-600" />
                          )}
                        </div>
                      </td>
                      <td className="px-4 py-3 max-w-xs truncate font-medium text-slate-800 dark:text-slate-200">
                        {item.filename}
                      </td>
                      <td className="px-4 py-3 text-slate-600 dark:text-slate-400">
                        {item.spir_no ?? "—"}
                      </td>
                      <td className="px-4 py-3 text-right tabular-nums text-slate-600 dark:text-slate-400">
                        {item.total_rows ?? "—"}
                      </td>
                      <td className="px-4 py-3 text-right tabular-nums text-slate-600 dark:text-slate-400">
                        {item.tag_count}
                      </td>
                      <td className="px-4 py-3 text-right tabular-nums text-slate-600 dark:text-slate-400">
                        {item.spare_count}
                      </td>
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
                  );
                })}
              </tbody>
            </table>

            {/* Selection status bar */}
            {selected.size > 0 && (
              <div className="border-t border-slate-100 dark:border-slate-700 bg-violet-50 dark:bg-violet-900/10 px-4 py-2 text-xs text-violet-700 dark:text-violet-300">
                {selected.size} file{selected.size > 1 ? "s" : ""} selected
                {" · "}
                <button
                  onClick={() => setSelected(new Set())}
                  className="underline hover:no-underline"
                >
                  Clear selection
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </SidebarLayout>
  );
}
