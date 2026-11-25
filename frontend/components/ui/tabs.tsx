"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

export interface TabItem<T extends string> {
  value: T;
  label: string;
  icon?: React.ElementType;
  /** Optional count shown as a pill (hidden when 0/undefined). */
  count?: number;
}

interface TabsProps<T extends string> {
  items: TabItem<T>[];
  value: T;
  onValueChange: (value: T) => void;
  className?: string;
  "aria-label"?: string;
}

/** Underlined text tab bar (role="tablist") with ←/→ keyboard navigation. */
function Tabs<T extends string>({ items, value, onValueChange, className, ...aria }: TabsProps<T>) {
  const refs = React.useRef<(HTMLButtonElement | null)[]>([]);

  function onKeyDown(e: React.KeyboardEvent, index: number) {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const next = (index + (e.key === "ArrowRight" ? 1 : -1) + items.length) % items.length;
    refs.current[next]?.focus();
    onValueChange(items[next].value);
  }

  return (
    <div
      role="tablist"
      aria-label={aria["aria-label"]}
      className={cn("flex items-center gap-7 border-b border-slate-200", className)}
    >
      {items.map((item, i) => {
        const active = item.value === value;
        const Icon = item.icon;
        return (
          <button
            key={item.value}
            ref={(el) => { refs.current[i] = el; }}
            type="button"
            role="tab"
            id={`tab-${item.value}`}
            aria-selected={active}
            aria-controls={`tabpanel-${item.value}`}
            tabIndex={active ? 0 : -1}
            onClick={() => onValueChange(item.value)}
            onKeyDown={(e) => onKeyDown(e, i)}
            className={cn(
              // -mb-px: the active underline sits on the bar's bottom border.
              "-mb-px inline-flex h-11 items-center gap-2 border-b-2 text-sm transition-colors",
              "focus-visible:outline-none focus-visible:text-slate-900",
              active
                ? "border-brand-700 font-semibold text-slate-900"
                : "border-transparent font-medium text-slate-500 hover:text-slate-800"
            )}
          >
            {Icon && <Icon className="h-4 w-4" />}
            {item.label}
            {!!item.count && (
              <span className="min-w-[1.25rem] rounded-full bg-cdc-gold/25 px-1.5 text-center text-[11px] font-bold leading-5 tabular-nums text-amber-800">
                {item.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

/** Panel wrapper paired with <Tabs> for screen readers. */
function TabPanel({ value, children, className }: { value: string; children: React.ReactNode; className?: string }) {
  return (
    <div role="tabpanel" id={`tabpanel-${value}`} aria-labelledby={`tab-${value}`} className={className}>
      {children}
    </div>
  );
}

export { Tabs, TabPanel };
