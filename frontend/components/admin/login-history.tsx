"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertCircle, ChevronLeft, ChevronRight, Search } from "lucide-react";
import { authHeaders } from "@/lib/auth";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, inputClassName } from "@/components/ui/input";

// Audit log: one row per sign-in, kept deliberately simple —
// When · User · Branch · Signed out · Extracted · Status.

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const PAGE_SIZE = 25;

interface LoginRow {
  session_id: string;
  username: string;
  branch_name: string | null;
  login_at: string;
  ended_at: string | null;
  status: "active" | "signed_out" | "expired";
  extractions: number;
}

const STATUS = {
  active: { label: "active", variant: "green" as const },
  signed_out: { label: "signed out", variant: "slate" as const },
  expired: { label: "expired", variant: "amber" as const },
};

// Date filter presets → [from, to) as local-day bounds (to is exclusive, like the API).
type DatePreset = "any" | "today" | "yesterday" | "7d" | "30d" | "custom";
const startOfLocalDay = (d: Date, addDays = 0) => {
  const x = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  x.setDate(x.getDate() + addDays);
  return x;
};
const fromInput = (v: string, addDays = 0) => {
  const [y, m, d] = v.split("-").map(Number);
  return startOfLocalDay(new Date(y, m - 1, d), addDays);
};
function dateRange(preset: DatePreset, from: string, to: string): [Date | null, Date | null] {
  const today = startOfLocalDay(new Date());
  switch (preset) {
    case "today": return [today, startOfLocalDay(today, 1)];
    case "yesterday": return [startOfLocalDay(today, -1), today];
    case "7d": return [startOfLocalDay(today, -6), startOfLocalDay(today, 1)];
    case "30d": return [startOfLocalDay(today, -29), startOfLocalDay(today, 1)];
    case "custom": return [from ? fromInput(from) : null, to ? fromInput(to, 1) : null];
    default: return [null, null];
  }
}

const TZ = { timeZone: "Asia/Kolkata" } as const;
const dateOf = (iso: string) => new Date(iso).toLocaleDateString("en-US", { ...TZ, month: "short", day: "numeric", year: "numeric" });
const timeOf = (iso: string) => new Date(iso).toLocaleTimeString("en-US", { ...TZ, hour: "2-digit", minute: "2-digit" });

/** Sign-out time; prefixed with its date when it falls on a different day than sign-in. */
function signedOut(loginIso: string, endIso: string | null) {
  if (!endIso) return "—";
  return dateOf(endIso) === dateOf(loginIso) ? timeOf(endIso) : `${dateOf(endIso)}, ${timeOf(endIso)}`;
}

export function LoginHistory({ branches }: { branches: { id: string; name: string }[] }) {
  const [search, setSearch] = useState("");
  const [debounced, setDebounced] = useState("");
  const [branch, setBranch] = useState("all");
  const [role, setRole] = useState("all");
  const [status, setStatus] = useState("all");
  const [datePreset, setDatePreset] = useState<DatePreset>("any");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [page, setPage] = useState(0);

  const [rows, setRows] = useState<LoginRow[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const t = setTimeout(() => setDebounced(search.trim()), 300);
    return () => clearTimeout(t);
  }, [search]);

  useEffect(() => setPage(0), [debounced, branch, role, status, datePreset, from, to]);

  const query = useMemo(() => {
    const p = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(page * PAGE_SIZE) });
    if (debounced) p.set("q", debounced);
    if (branch !== "all") p.set("branch_id", branch);
    if (role !== "all") p.set("role", role);
    if (status !== "all") p.set("status_filter", status);
    const [df, dt] = dateRange(datePreset, from, to);
    if (df) p.set("date_from", df.toISOString());
    if (dt) p.set("date_to", dt.toISOString());
    return p.toString();
  }, [debounced, branch, role, status, datePreset, from, to, page]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_URL}/api/admin/login-history?${query}`, { headers: { ...authHeaders() } });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setError(data.detail ?? `Could not load the audit log (${res.status}).`);
        return;
      }
      const data = await res.json();
      setRows(data.items ?? []);
      setTotal(data.total ?? 0);
    } catch {
      setError("Network error. Is the backend running?");
    } finally {
      setLoading(false);
    }
  }, [query]);

  useEffect(() => { load(); }, [load]);

  const selectCls = cn(inputClassName, "h-10 w-auto min-w-[11rem] cursor-pointer bg-white");
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const filtered = debounced !== "" || branch !== "all" || role !== "all" || status !== "all" || datePreset !== "any";

  return (
    <div className="space-y-4">
      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative w-full sm:w-64">
          <Search className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search user"
            aria-label="Search user"
            className="bg-white pl-10"
          />
        </div>
        <select value={branch} onChange={(e) => setBranch(e.target.value)} aria-label="Branch" className={selectCls}>
          <option value="all">All branches</option>
          <option value="none">No branch</option>
          {branches.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
        </select>
        <select value={role} onChange={(e) => setRole(e.target.value)} aria-label="Role" className={selectCls}>
          <option value="all">All roles</option>
          <option value="super_admin">Super Admin</option>
          <option value="branch_admin">Branch Admin</option>
          <option value="user">User</option>
        </select>
        <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status" className={selectCls}>
          <option value="all">All statuses</option>
          <option value="active">Active</option>
          <option value="signed_out">Signed out</option>
          <option value="expired">Expired</option>
        </select>
        <select
          value={datePreset}
          onChange={(e) => setDatePreset(e.target.value as DatePreset)}
          aria-label="Date"
          className={selectCls}
        >
          <option value="any">Any date</option>
          <option value="today">Today</option>
          <option value="yesterday">Yesterday</option>
          <option value="7d">Last 7 days</option>
          <option value="30d">Last 30 days</option>
          <option value="custom">Custom range…</option>
        </select>
        {datePreset === "custom" && (
          <div className="flex items-center gap-2">
            <input
              type="date"
              value={from}
              max={to || undefined}
              onChange={(e) => setFrom(e.target.value)}
              aria-label="From date"
              className={cn(selectCls, "min-w-0")}
            />
            <span className="text-sm text-slate-400">–</span>
            <input
              type="date"
              value={to}
              min={from || undefined}
              onChange={(e) => setTo(e.target.value)}
              aria-label="To date"
              className={cn(selectCls, "min-w-0")}
            />
          </div>
        )}
      </div>

      {error && (
        <div className="flex items-start gap-2.5 rounded-xl bg-red-50 p-3 text-sm text-red-700">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          {error}
        </div>
      )}

      <Card className="rounded-2xl">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-100">
                {["When", "User", "Branch", "Signed in", "Signed out", "Extracted", "Status"].map((h) => (
                  <th key={h} className="whitespace-nowrap px-5 py-3 text-left text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className={cn("divide-y divide-slate-100", loading && "opacity-60")}>
              {!loading && rows.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-5 py-12 text-center text-sm text-slate-500">
                    {filtered ? "No sign-ins match these filters." : "No sign-ins recorded yet."}
                  </td>
                </tr>
              )}
              {rows.map((r) => {
                const st = STATUS[r.status] ?? STATUS.signed_out;
                return (
                  <tr key={r.session_id} className="hover:bg-slate-50/70">
                    <td className="whitespace-nowrap px-5 py-3 text-slate-500">{dateOf(r.login_at)}</td>
                    <td className="px-5 py-3 font-semibold text-slate-900">{r.username}</td>
                    <td className="px-5 py-3 text-slate-600">{r.branch_name ?? "—"}</td>
                    <td className="whitespace-nowrap px-5 py-3 tabular-nums text-slate-700">{timeOf(r.login_at)}</td>
                    <td className="whitespace-nowrap px-5 py-3 tabular-nums text-slate-500">{signedOut(r.login_at, r.ended_at)}</td>
                    <td className="px-5 py-3 tabular-nums text-slate-900">{r.extractions}</td>
                    <td className="px-5 py-3">
                      <Badge variant={st.variant} className="gap-1.5">
                        <span className="h-1.5 w-1.5 rounded-full bg-current" />
                        {st.label}
                      </Badge>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        {pages > 1 && (
          <div className="flex items-center justify-between gap-3 border-t border-slate-100 px-5 py-3 text-xs text-slate-500">
            <span className="tabular-nums">{total.toLocaleString()} sign-ins</span>
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" onClick={() => setPage((p) => p - 1)} disabled={page === 0 || loading}>
                <ChevronLeft />
                Previous
              </Button>
              <span className="tabular-nums">{page + 1} / {pages}</span>
              <Button variant="outline" size="sm" onClick={() => setPage((p) => p + 1)} disabled={page + 1 >= pages || loading}>
                Next
                <ChevronRight />
              </Button>
            </div>
          </div>
        )}
      </Card>
    </div>
  );
}
