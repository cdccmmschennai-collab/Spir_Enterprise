"use client";

// Searchable select for the controlled Planning Plant list.
//
// Closed: a field-sized trigger button (opens the list) with an optional
// clear "×" button layered above it — two sibling buttons, never nested.
// Open: a search box at the top of the popup takes focus and drives the
// listbox via aria-activedescendant (WAI-ARIA combobox with listbox popup).
// Keys: ↑/↓, Home/End, Enter, Esc, Tab; Enter/Space/↑/↓ open it from the trigger.
// Only the nine controlled plants can be chosen — the search text is a filter,
// never a value.

import { forwardRef, useCallback, useEffect, useId, useRef, useState } from "react";
import { AlertCircle, Check, ChevronDown, Search, X } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  filterPlanningPlants,
  planningPlantLabel,
  type PlanningPlant,
} from "@/lib/planning-plants";

interface PlanningPlantSelectProps {
  value: PlanningPlant | null;
  onChange: (plant: PlanningPlant | null) => void;
  invalid?: boolean;
  disabled?: boolean;
  className?: string;
  describedBy?: string;
}

export const PlanningPlantSelect = forwardRef<HTMLButtonElement, PlanningPlantSelectProps>(
  function PlanningPlantSelect({ value, onChange, invalid, disabled, className, describedBy }, ref) {
    const id = useId();
    const listId = `${id}-list`;
    const optionId = (code: string) => `${id}-opt-${code}`;
    const rootRef = useRef<HTMLDivElement>(null);
    const triggerRef = useRef<HTMLButtonElement | null>(null);
    const searchRef = useRef<HTMLInputElement>(null);
    const listRef = useRef<HTMLUListElement>(null);
    const [open, setOpen] = useState(false);
    const [query, setQuery] = useState("");
    const [active, setActive] = useState(-1);

    const options = filterPlanningPlants(query);
    const activePlant = active >= 0 ? options[active] : undefined;

    const setTriggerRef = useCallback(
      (el: HTMLButtonElement | null) => {
        triggerRef.current = el;
        if (typeof ref === "function") ref(el);
        else if (ref) ref.current = el;
      },
      [ref]
    );

    const openList = useCallback(() => {
      if (disabled) return;
      const all = filterPlanningPlants("");
      const sel = value ? all.findIndex((p) => p.code === value.code) : -1;
      setQuery("");
      setActive(sel >= 0 ? sel : 0);
      setOpen(true);
    }, [disabled, value]);

    // Close and hand focus back to the trigger (the search box unmounts).
    const close = useCallback(() => {
      setOpen(false);
      triggerRef.current?.focus();
    }, []);

    const commit = useCallback(
      (plant: PlanningPlant | undefined) => {
        if (!plant) return;
        onChange(plant);
        close();
      },
      [onChange, close]
    );

    const clear = () => {
      onChange(null);
      triggerRef.current?.focus();
    };

    // The search box takes focus as soon as the popup opens.
    useEffect(() => {
      if (open) searchRef.current?.focus();
    }, [open]);

    // Click / tap outside closes without changing the selection.
    useEffect(() => {
      if (!open) return;
      const onDown = (e: PointerEvent) => {
        if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
      };
      document.addEventListener("pointerdown", onDown);
      return () => document.removeEventListener("pointerdown", onDown);
    }, [open]);

    // Keep the highlighted option visible.
    useEffect(() => {
      if (!open || active < 0) return;
      listRef.current?.children[active]?.scrollIntoView({ block: "nearest" });
    }, [open, active]);

    const onTriggerKeyDown = (e: React.KeyboardEvent<HTMLButtonElement>) => {
      if (disabled || open) return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openList();
      }
    };

    const onSearchKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
      const last = options.length - 1;
      switch (e.key) {
        case "ArrowDown":
          e.preventDefault();
          if (last >= 0) setActive((a) => Math.min(last, a + 1));
          return;
        case "ArrowUp":
          e.preventDefault();
          if (last >= 0) setActive((a) => Math.max(0, a - 1));
          return;
        case "Home":
          if (last >= 0) { e.preventDefault(); setActive(0); }
          return;
        case "End":
          if (last >= 0) { e.preventDefault(); setActive(last); }
          return;
        case "Enter":
          e.preventDefault();
          commit(activePlant);
          return;
        case "Escape":
          e.preventDefault();
          close();
          return;
        case "Tab":
          // Focus returns to the trigger first, so Tab continues from the field.
          close();
          return;
      }
    };

    return (
      <div ref={rootRef} className={cn("relative", className)}>
        {/* Field: the trigger covers it; text/chevron are decoration; × sits above. */}
        <div
          className={cn(
            "relative flex h-10 w-full items-center gap-1 rounded-xl border bg-white pl-3.5 pr-2.5 text-sm shadow-sm transition-colors dark:bg-slate-800",
            invalid
              ? "border-red-300 bg-red-50/40 dark:border-red-800 dark:bg-red-950/10"
              : open
              ? "border-violet-400 ring-2 ring-violet-400/30 dark:border-violet-500"
              : "border-slate-200 hover:border-slate-300 dark:border-slate-600 dark:hover:border-slate-500",
            disabled && "opacity-60"
          )}
        >
          <button
            ref={setTriggerRef}
            type="button"
            aria-haspopup="listbox"
            aria-expanded={open}
            aria-controls={open ? listId : undefined}
            aria-label={value ? `Planning Plant: ${planningPlantLabel(value)}` : "Select Planning Plant"}
            aria-required
            aria-invalid={invalid || undefined}
            aria-describedby={describedBy}
            disabled={disabled}
            onClick={() => (open ? close() : openList())}
            onKeyDown={onTriggerKeyDown}
            className={cn(
              "absolute inset-0 rounded-xl focus-visible:outline-none focus-visible:ring-2",
              invalid ? "focus-visible:ring-red-400/40" : "focus-visible:ring-violet-400/40",
              disabled ? "cursor-not-allowed" : "cursor-pointer"
            )}
          />
          <span
            aria-hidden
            className={cn(
              "pointer-events-none min-w-0 flex-1 truncate",
              value
                ? "font-medium text-slate-800 dark:text-slate-200"
                : invalid
                ? "text-red-500 dark:text-red-400"
                : "text-slate-400 dark:text-slate-500"
            )}
          >
            {value ? planningPlantLabel(value) : "Select Planning Plant"}
          </span>
          {value && !disabled && (
            <button
              type="button"
              aria-label="Clear Planning Plant"
              title="Clear Planning Plant"
              onClick={clear}
              className="relative z-10 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-400/40 dark:text-slate-500 dark:hover:bg-slate-700 dark:hover:text-slate-200"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
          <ChevronDown
            aria-hidden
            className={cn(
              "pointer-events-none h-4 w-4 shrink-0 text-slate-400 transition-transform duration-150 dark:text-slate-500",
              open && "rotate-180 text-violet-600 dark:text-violet-400"
            )}
          />
        </div>

        {open && (
          <div className="absolute left-0 right-0 top-full z-30 mt-1.5 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-lg shadow-slate-200/60 dark:border-slate-700 dark:bg-slate-800 dark:shadow-black/30">
            <div className="flex items-center gap-2 border-b border-slate-100 px-3 dark:border-slate-700">
              <Search className="h-3.5 w-3.5 shrink-0 text-slate-400" aria-hidden />
              <input
                ref={searchRef}
                type="text"
                role="combobox"
                aria-expanded
                aria-controls={listId}
                aria-autocomplete="list"
                aria-activedescendant={activePlant ? optionId(activePlant.code) : undefined}
                aria-label="Search planning plant"
                placeholder="Search planning plant..."
                autoComplete="off"
                spellCheck={false}
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  setActive(filterPlanningPlants(e.target.value).length ? 0 : -1);
                }}
                onKeyDown={onSearchKeyDown}
                className="h-10 w-full bg-transparent text-sm text-slate-800 placeholder:text-slate-400 focus:outline-none dark:text-slate-200 dark:placeholder:text-slate-500"
              />
            </div>
            <ul
              ref={listRef}
              id={listId}
              role="listbox"
              aria-label="Planning Plant"
              className="max-h-72 overflow-auto p-1"
            >
              {options.map((p, i) => {
                const selected = value?.code === p.code;
                return (
                  <li
                    key={p.code}
                    id={optionId(p.code)}
                    role="option"
                    aria-selected={selected}
                    // Keep focus in the search box while clicking an option.
                    onMouseDown={(e) => e.preventDefault()}
                    onMouseMove={() => active !== i && setActive(i)}
                    onClick={() => commit(p)}
                    className={cn(
                      "flex cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm",
                      i === active ? "bg-violet-50 dark:bg-violet-950/40" : "bg-transparent",
                      selected
                        ? "font-semibold text-violet-700 dark:text-violet-300"
                        : "text-slate-700 dark:text-slate-300"
                    )}
                  >
                    <span className="min-w-0 flex-1 truncate">{planningPlantLabel(p)}</span>
                    <Check
                      aria-hidden
                      className={cn("h-4 w-4 shrink-0 text-violet-600 dark:text-violet-400", !selected && "invisible")}
                    />
                  </li>
                );
              })}
            </ul>
            {options.length === 0 && (
              <p role="status" className="px-3.5 pb-3 pt-1 text-xs text-slate-400 dark:text-slate-500">
                No planning plants found
              </p>
            )}
          </div>
        )}
      </div>
    );
  }
);

// Compact required-field message shown under the selector.
export function PlanningPlantError({ id, className }: { id: string; className?: string }) {
  return (
    <div
      id={id}
      role="alert"
      className={cn(
        "flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-400",
        className
      )}
    >
      <AlertCircle className="mt-px h-4 w-4 shrink-0" aria-hidden />
      <div className="min-w-0">
        <p className="text-xs font-semibold">Planning Plant required</p>
        <p className="mt-0.5 text-xs text-red-600/90 dark:text-red-400/90">
          Please select a Planning Plant before starting the extraction.
        </p>
      </div>
    </div>
  );
}
