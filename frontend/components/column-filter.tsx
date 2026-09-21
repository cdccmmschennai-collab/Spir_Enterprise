"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  ArrowDown,
  ArrowDown01,
  ArrowDownAZ,
  ArrowUp,
  ArrowUp10,
  ArrowUpZA,
  ChevronDown,
  Filter,
  FilterX,
  Search,
} from "lucide-react";
import { cn } from "@/lib/utils";

// ─── Value helpers (shared with the table's filter/sort pipeline) ──────────────

export type CellValue = string | number | null;
export type SortDir = "asc" | "desc";

/** Internal key for blank/null cells — rendered as "(Blanks)" in the menu. */
export const BLANK_KEY = "\u0000blank";
const BLANK_LABEL = "(Blanks)";

export function isBlank(v: CellValue | undefined): boolean {
  return v === null || v === undefined || (typeof v === "string" && v.trim() === "");
}

/** Canonical string key for a cell, so 2 and "2" filter as the same value. */
export function valueKey(v: CellValue | undefined): string {
  return isBlank(v) ? BLANK_KEY : String(v);
}

function keyLabel(k: string): string {
  return k === BLANK_KEY ? BLANK_LABEL : k;
}

function toNumber(v: CellValue): number | null {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  const s = String(v).trim();
  if (s === "") return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

/** A column is numeric when every non-blank value parses as a finite number. */
export function isNumericColumn(values: CellValue[]): boolean {
  let seen = false;
  for (const v of values) {
    if (isBlank(v)) continue;
    if (toNumber(v) === null) return false;
    seen = true;
  }
  return seen;
}

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

/** Excel semantics: blanks always sort last regardless of direction. */
export function compareValues(a: CellValue, b: CellValue, numeric: boolean, dir: SortDir): number {
  const aBlank = isBlank(a);
  const bBlank = isBlank(b);
  if (aBlank && bBlank) return 0;
  if (aBlank) return 1;
  if (bBlank) return -1;
  const r = numeric
    ? (toNumber(a) as number) - (toNumber(b) as number)
    : collator.compare(String(a), String(b));
  return dir === "asc" ? r : -r;
}

function keyToValue(k: string): CellValue {
  return k === BLANK_KEY ? null : k;
}

// ─── Column filter control (header trigger + Excel-style AutoFilter menu) ──────

interface ColumnFilterProps {
  label: string;
  numeric: boolean;
  /** Selected value keys, or null when the column has no filter. */
  selected: Set<string> | null;
  sortDir: SortDir | null;
  /** Values to list — rows passing every OTHER column's filter. Called only when the menu opens. */
  getValues: () => CellValue[];
  onApply: (selected: Set<string> | null) => void;
  onSort: (dir: SortDir) => void;
}

export function ColumnFilter({ label, numeric, selected, sortDir, getValues, onApply, onSort }: ColumnFilterProps) {
  const [open, setOpen] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);
  const filtered = selected !== null;

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-label={`Filter ${label}`}
        aria-haspopup="menu"
        aria-expanded={open}
        className={cn(
          "flex h-5 w-5 shrink-0 items-center justify-center rounded border transition-colors",
          filtered
            ? "border-violet-700 bg-violet-700 text-white"
            : "border-slate-200 bg-white text-slate-500 hover:border-violet-400 hover:text-violet-700 dark:border-slate-600 dark:bg-slate-700 dark:text-slate-300 dark:hover:text-violet-400",
          !filtered && sortDir && "text-violet-700 dark:text-violet-400"
        )}
      >
        {filtered ? (
          <Filter className="h-3 w-3" />
        ) : sortDir === "asc" ? (
          <ArrowUp className="h-3 w-3" />
        ) : sortDir === "desc" ? (
          <ArrowDown className="h-3 w-3" />
        ) : (
          <ChevronDown className="h-3 w-3" />
        )}
      </button>
      {open && (
        <FilterMenu
          anchor={btnRef.current}
          numeric={numeric}
          selected={selected}
          values={getValues()}
          onClose={() => setOpen(false)}
          onApply={(sel) => {
            onApply(sel);
            setOpen(false);
          }}
          onSort={(dir) => {
            onSort(dir);
            setOpen(false);
          }}
        />
      )}
    </>
  );
}

interface FilterMenuProps {
  anchor: HTMLElement | null;
  numeric: boolean;
  selected: Set<string> | null;
  values: CellValue[];
  onClose: () => void;
  onApply: (selected: Set<string> | null) => void;
  onSort: (dir: SortDir) => void;
}

const MENU_WIDTH = 288; // w-72
const MENU_MAX_HEIGHT = 400;

function FilterMenu({ anchor, numeric, selected, values, onClose, onApply, onSort }: FilterMenuProps) {
  const menuRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const [term, setTerm] = useState("");

  // Unique values for this column, sorted per column type, blanks last.
  const uniqueKeys = useMemo(() => {
    const keys = Array.from(new Set(values.map(valueKey)));
    keys.sort((a, b) => compareValues(keyToValue(a), keyToValue(b), numeric, "asc"));
    return keys;
  }, [values, numeric]);

  // Draft selection — only committed on OK; Cancel/outside click discards it.
  const initialDraft = useMemo(
    () => (selected === null ? new Set(uniqueKeys) : new Set(selected)),
    [selected, uniqueKeys]
  );
  const [draft, setDraft] = useState<Set<string>>(initialDraft);

  const listedKeys = useMemo(() => {
    const t = term.trim().toLowerCase();
    if (!t) return uniqueKeys;
    return uniqueKeys.filter((k) => keyLabel(k).toLowerCase().includes(t));
  }, [uniqueKeys, term]);

  const allListedChecked = listedKeys.length > 0 && listedKeys.every((k) => draft.has(k));
  const someListedChecked = listedKeys.some((k) => draft.has(k));
  const selectAllRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (selectAllRef.current) selectAllRef.current.indeterminate = someListedChecked && !allListedChecked;
  }, [someListedChecked, allListedChecked]);

  // Anchor below the header cell (aligned to its left edge), kept inside the viewport.
  useLayoutEffect(() => {
    const cell = anchor?.closest("th") ?? anchor;
    if (!cell) return;
    const r = cell.getBoundingClientRect();
    let left = r.left;
    if (left + MENU_WIDTH > window.innerWidth - 8) left = Math.max(8, r.right - MENU_WIDTH);
    let top = r.bottom + 4;
    if (top + MENU_MAX_HEIGHT > window.innerHeight - 8) top = Math.max(8, r.top - MENU_MAX_HEIGHT - 4);
    setPos({ top, left });
  }, [anchor]);

  // Close on outside click, Escape, or any scroll outside the menu (position is fixed).
  useEffect(() => {
    function onPointer(e: MouseEvent) {
      const t = e.target as Node;
      if (menuRef.current?.contains(t) || anchor?.contains(t)) return;
      onClose();
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault(); // consumed here so outer Escape handlers (e.g. fullscreen preview) don't also fire
        onClose();
      }
    }
    function onScroll(e: Event) {
      if (menuRef.current?.contains(e.target as Node)) return;
      onClose();
    }
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    document.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onClose);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onClose);
    };
  }, [anchor, onClose]);

  function onSearch(next: string) {
    setTerm(next);
    const t = next.trim().toLowerCase();
    // Excel behaviour: a search pre-selects exactly the matching values; clearing it restores the opened state.
    setDraft(t ? new Set(uniqueKeys.filter((k) => keyLabel(k).toLowerCase().includes(t))) : new Set(initialDraft));
  }

  function toggleAll() {
    setDraft((prev) => {
      const next = new Set(prev);
      if (allListedChecked) listedKeys.forEach((k) => next.delete(k));
      else listedKeys.forEach((k) => next.add(k));
      return next;
    });
  }

  function toggleOne(k: string) {
    setDraft((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });
  }

  function apply() {
    // Every value selected == no filter on this column.
    const all = uniqueKeys.every((k) => draft.has(k));
    onApply(all ? null : new Set(draft));
  }

  if (typeof document === "undefined") return null;

  const itemClass =
    "flex w-full items-center gap-2.5 rounded-lg px-2 py-1.5 text-left text-xs text-slate-700 hover:bg-slate-50 dark:text-slate-200 dark:hover:bg-slate-700/60 transition-colors";

  return createPortal(
    <div
      ref={menuRef}
      role="menu"
      style={{ position: "fixed", top: pos?.top ?? 0, left: pos?.left ?? 0, width: MENU_WIDTH, visibility: pos ? "visible" : "hidden" }}
      className="z-50 rounded-xl border border-slate-200 bg-white p-2 shadow-lg dark:border-slate-700 dark:bg-slate-800 animate-fade-in"
    >
      {/* Sort */}
      <button type="button" className={itemClass} onClick={() => onSort("asc")}>
        {numeric ? <ArrowDown01 className="h-3.5 w-3.5 text-slate-500" /> : <ArrowDownAZ className="h-3.5 w-3.5 text-slate-500" />}
        {numeric ? "Sort Smallest to Largest" : "Sort A to Z"}
      </button>
      <button type="button" className={itemClass} onClick={() => onSort("desc")}>
        {numeric ? <ArrowUp10 className="h-3.5 w-3.5 text-slate-500" /> : <ArrowUpZA className="h-3.5 w-3.5 text-slate-500" />}
        {numeric ? "Sort Largest to Smallest" : "Sort Z to A"}
      </button>

      <div className="my-2 border-t border-slate-200 dark:border-slate-700" />

      {/* Clear filter — this column only; other columns' filters are untouched. */}
      <button
        type="button"
        className={cn(itemClass, "disabled:cursor-default disabled:opacity-40 disabled:hover:bg-transparent")}
        disabled={selected === null}
        onClick={() => onApply(null)}
      >
        <FilterX className="h-3.5 w-3.5 text-slate-500" />
        Clear Filter
      </button>

      <div className="my-2 border-t border-slate-200 dark:border-slate-700" />

      {/* Filter by value */}
      <div className="flex items-center gap-2.5 px-2 py-1 text-xs font-medium text-slate-700 dark:text-slate-200">
        <Filter className="h-3.5 w-3.5 text-slate-500" />
        Filter by Value
      </div>
      <div className="relative mt-1 px-2">
        <Search className="pointer-events-none absolute left-4 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-400" />
        <input
          autoFocus
          value={term}
          onChange={(e) => onSearch(e.target.value)}
          placeholder="Search..."
          className="h-8 w-full rounded-lg border border-input bg-background pl-7 pr-2 text-xs placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500 dark:bg-slate-900"
        />
      </div>

      <div className="mt-2 max-h-48 overflow-y-auto px-1">
        <label className={cn(itemClass, "cursor-pointer")}>
          <input
            ref={selectAllRef}
            type="checkbox"
            checked={allListedChecked}
            onChange={toggleAll}
            disabled={listedKeys.length === 0}
            className="h-3.5 w-3.5 rounded border-slate-300 accent-violet-700"
          />
          (Select All)
        </label>
        {listedKeys.length === 0 ? (
          <p className="px-2 py-3 text-center text-xs text-slate-400 dark:text-slate-500">No matching values</p>
        ) : (
          listedKeys.map((k) => (
            <label key={k} className={cn(itemClass, "cursor-pointer")} title={keyLabel(k)}>
              <input
                type="checkbox"
                checked={draft.has(k)}
                onChange={() => toggleOne(k)}
                className="h-3.5 w-3.5 shrink-0 rounded border-slate-300 accent-violet-700"
              />
              <span className={cn("truncate", k === BLANK_KEY && "italic text-slate-500 dark:text-slate-400")}>
                {keyLabel(k)}
              </span>
            </label>
          ))
        )}
      </div>

      {/* Actions */}
      <div className="mt-2 flex items-center gap-2 border-t border-slate-200 px-1 pt-2 dark:border-slate-700">
        <button
          type="button"
          onClick={apply}
          className="flex h-8 flex-1 items-center justify-center rounded-lg bg-violet-700 text-xs font-semibold text-white shadow-sm transition-colors hover:bg-violet-800"
        >
          OK
        </button>
        <button
          type="button"
          onClick={onClose}
          className="flex h-8 flex-1 items-center justify-center rounded-lg border border-slate-200 bg-white text-xs font-medium text-slate-700 transition-colors hover:bg-slate-50 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-200 dark:hover:bg-slate-700"
        >
          Cancel
        </button>
      </div>
    </div>,
    document.body
  );
}
