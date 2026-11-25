"use client";

import * as React from "react";
import { ArrowDownRight, ArrowUpRight, Minus } from "lucide-react";
import { cn } from "@/lib/utils";

// KPI stat tile (dataviz "stat tile" contract):
//   label (sentence case) · value (semibold, auto-compact) · optional context:
//   a delta vs a named period, a share meter, or a sparkline trend.
// Text always wears text tokens; colour lives on the marks beside it.

/** 1,284 below 10k; 12.9K / 4.2M above. */
export function formatCompact(n: number) {
  return n >= 10_000
    ? new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(n)
    : n.toLocaleString("en");
}

interface StatCardProps {
  label: string;
  value: number;
  icon?: React.ElementType;
  /** Context row under the value (Delta, Meter, Sparkline, or plain text). */
  children?: React.ReactNode;
  className?: string;
}

function StatCard({ label, value, icon: Icon, children, className }: StatCardProps) {
  return (
    <div
      className={cn(
        "flex min-h-[132px] flex-col rounded-2xl border border-slate-200/80 bg-white p-5",
        "shadow-[0_12px_32px_-20px_rgba(15,23,42,0.18)]",
        className
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <p className="text-[13px] font-medium text-slate-500">{label}</p>
        {Icon && <Icon className="h-4 w-4 shrink-0 text-slate-400" aria-hidden="true" />}
      </div>
      <p className="mt-2 text-[28px] font-semibold leading-none tracking-[-0.02em] tabular-nums text-slate-900" title={value.toLocaleString("en")}>
        {formatCompact(value)}
      </p>
      {children && <div className="mt-auto pt-4">{children}</div>}
    </div>
  );
}

/** Muted one-line context. */
function StatNote({ children }: { children: React.ReactNode }) {
  return <p className="text-xs text-slate-500">{children}</p>;
}

/** Signed change vs a named period. Colour = direction × whether up is good; never colour alone (icon + text). */
function StatDelta({ current, previous, period, upIsGood = true }: { current: number; previous: number; period: string; upIsGood?: boolean }) {
  const diff = current - previous;
  if (diff === 0) {
    return (
      <p className="flex items-center gap-1 text-xs text-slate-500">
        <Minus className="h-3.5 w-3.5" aria-hidden="true" />
        Same as {period}
      </p>
    );
  }
  const up = diff > 0;
  const good = up === upIsGood;
  const Arrow = up ? ArrowUpRight : ArrowDownRight;
  return (
    <p className="flex items-center gap-1 text-xs text-slate-500">
      <span className={cn("inline-flex items-center gap-0.5 font-semibold", good ? "text-emerald-700" : "text-red-700")}>
        <Arrow className="h-3.5 w-3.5" aria-hidden="true" />
        {up ? "+" : "−"}
        {Math.abs(diff).toLocaleString("en")}
      </span>
      vs {period}
    </p>
  );
}

/** Share meter: navy fill on a lighter navy track, caption beside it. */
function StatMeter({ value, max, caption }: { value: number; max: number; caption: string }) {
  const pct = max > 0 ? Math.round((value / max) * 100) : 0;
  return (
    <div className="space-y-1.5">
      <div
        className="h-1.5 w-full overflow-hidden rounded-full bg-brand-100"
        role="meter"
        aria-valuemin={0}
        aria-valuemax={max}
        aria-valuenow={value}
        aria-label={caption}
      >
        <div className="h-full rounded-full bg-brand-700 transition-[width] duration-500" style={{ width: `${pct}%` }} />
      </div>
      <p className="text-xs text-slate-500">{caption.replace("{pct}", String(pct))}</p>
    </div>
  );
}

/**
 * 2px trend line in the de-emphasis hue with the latest point in brand navy.
 * Hover (or focus + ←/→) shows the day's value; a screen-reader summary is included.
 */
function StatSparkline({ points, caption }: { points: { date: string; count: number }[]; caption: string }) {
  const [hover, setHover] = React.useState<number | null>(null);
  const ref = React.useRef<HTMLDivElement>(null);
  if (points.length < 2) return <StatNote>{caption}</StatNote>;

  const H = 32;
  const max = Math.max(1, ...points.map((p) => p.count));
  const xAt = (i: number) => (i / (points.length - 1)) * 100; // % across
  const yAt = (v: number) => 2 + (1 - v / max) * (H - 4); // px down, 2px inset
  const path = points.map((p, i) => `${i ? "L" : "M"}${xAt(i)},${yAt(p.count)}`).join(" ");
  const last = points.length - 1;
  const active = hover ?? last;
  const fmtDay = (d: string) => new Date(`${d}T00:00:00Z`).toLocaleDateString("en", { month: "short", day: "numeric", timeZone: "UTC" });

  function pick(clientX: number) {
    const box = ref.current?.getBoundingClientRect();
    if (!box) return;
    const i = Math.round(((clientX - box.left) / box.width) * last);
    setHover(Math.max(0, Math.min(last, i)));
  }

  return (
    <div className="space-y-1.5">
      <div
        ref={ref}
        className="relative h-8 cursor-crosshair outline-none"
        tabIndex={0}
        role="img"
        aria-label={`${caption}: ${points.map((p) => `${fmtDay(p.date)} ${p.count}`).join(", ")}`}
        onMouseMove={(e) => pick(e.clientX)}
        onMouseLeave={() => setHover(null)}
        onBlur={() => setHover(null)}
        onKeyDown={(e) => {
          if (e.key === "ArrowLeft") setHover((h) => Math.max(0, (h ?? last) - 1));
          if (e.key === "ArrowRight") setHover((h) => Math.min(last, (h ?? last) + 1));
        }}
      >
        <svg viewBox={`0 0 100 ${H}`} preserveAspectRatio="none" className="h-full w-full overflow-visible" aria-hidden="true">
          <path d={path} fill="none" stroke="#CBD5E1" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
        </svg>
        {/* Point markers as HTML so they stay round on the stretched SVG. */}
        {hover !== null && hover !== last && (
          <span
            className="pointer-events-none absolute h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full bg-slate-500 ring-2 ring-white"
            style={{ left: `${xAt(hover)}%`, top: yAt(points[hover].count) }}
          />
        )}
        <span
          className="pointer-events-none absolute h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-brand-700 ring-2 ring-white"
          style={{ left: `${xAt(last)}%`, top: yAt(points[last].count) }}
        />
        {hover !== null && (
          <span
            className="pointer-events-none absolute -top-9 z-10 -translate-x-1/2 whitespace-nowrap rounded-lg bg-slate-900 px-2 py-1 text-[11px] font-medium text-white shadow-lg"
            style={{ left: `${Math.min(88, Math.max(12, xAt(hover)))}%` }}
          >
            {fmtDay(points[hover].date)} · {points[hover].count.toLocaleString("en")}
          </span>
        )}
      </div>
      <p className="text-xs text-slate-500">
        {hover === null ? caption : `${fmtDay(points[active].date)}: ${points[active].count.toLocaleString("en")} extracted`}
      </p>
    </div>
  );
}

export { StatCard, StatNote, StatDelta, StatMeter, StatSparkline };
