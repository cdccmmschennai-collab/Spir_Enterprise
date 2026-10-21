"use client";

import { useId, useRef, type KeyboardEvent } from "react";
import { cn } from "@/lib/utils";
import { EXTRACTION_MODES, type ExtractionMode } from "@/lib/extraction-mode";

interface ExtractionModeToggleProps {
  value: ExtractionMode;
  onChange: (mode: ExtractionMode) => void;
  disabled?: boolean;
  className?: string;
}

/**
 * Standard | Saipem segmented control.
 *
 * Accessible as a radio group: one tab stop (the selected option), arrow keys /
 * Home / End move and select, aria-checked marks the choice, and the helper
 * text describes the group (screen readers + hover tooltip — it sits in the
 * compact top header, see SidebarLayout's headerCenter). The highlight slides
 * between options with a short transform transition that is switched off for
 * reduced-motion users.
 */
export function ExtractionModeToggle({ value, onChange, disabled, className }: ExtractionModeToggleProps) {
  const descriptionId = useId();
  const optionRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const selectedIdx = Math.max(0, EXTRACTION_MODES.findIndex((m) => m.value === value));
  const count = EXTRACTION_MODES.length;

  const select = (idx: number, focus: boolean) => {
    const mode = EXTRACTION_MODES[idx].value;
    if (mode !== value) onChange(mode);
    if (focus) optionRefs.current[idx]?.focus();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    let next: number;
    switch (e.key) {
      case "ArrowRight":
      case "ArrowDown":
        next = (selectedIdx + 1) % count;
        break;
      case "ArrowLeft":
      case "ArrowUp":
        next = (selectedIdx - 1 + count) % count;
        break;
      case "Home":
        next = 0;
        break;
      case "End":
        next = count - 1;
        break;
      default:
        return;
    }
    e.preventDefault();
    select(next, true);
  };

  const description = EXTRACTION_MODES[selectedIdx].description;

  return (
    <div className={cn("w-52 shrink-0", className)}>
      {/* Compact capsule: soft grey track, white selected pill with a soft shadow */}
      <div
        role="radiogroup"
        aria-label="Extraction template"
        aria-describedby={descriptionId}
        aria-disabled={disabled || undefined}
        title={description}
        className={cn(
          "relative grid w-full grid-cols-2 rounded-full bg-slate-100 p-0.5 dark:bg-slate-800",
          disabled && "opacity-60"
        )}
      >
        {/* Sliding highlight behind the selected option */}
        <span
          aria-hidden
          className="pointer-events-none absolute inset-y-0.5 left-0.5 w-[calc(50%-0.125rem)] rounded-full bg-white shadow-[0_1px_2px_rgba(15,23,42,0.08),0_1px_4px_rgba(15,23,42,0.06)] transition-transform duration-200 ease-out motion-reduce:transition-none dark:bg-slate-600 dark:shadow-[0_1px_2px_rgba(0,0,0,0.35)]"
          style={{ transform: `translateX(${selectedIdx * 100}%)` }}
        />
        {EXTRACTION_MODES.map((mode, idx) => {
          const selected = idx === selectedIdx;
          return (
            <button
              key={mode.value}
              ref={(el) => { optionRefs.current[idx] = el; }}
              type="button"
              role="radio"
              aria-checked={selected}
              tabIndex={selected ? 0 : -1}
              disabled={disabled}
              data-mode={mode.value}
              onClick={() => select(idx, false)}
              onKeyDown={onKeyDown}
              className={cn(
                "relative z-10 h-8 rounded-full px-4 text-sm font-medium transition-colors duration-200 motion-reduce:transition-none",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400/60 dark:focus-visible:ring-slate-500/70",
                selected
                  ? "text-slate-900 dark:text-white"
                  : "text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200",
                disabled ? "cursor-not-allowed" : "cursor-pointer"
              )}
            >
              {mode.label}
            </button>
          );
        })}
      </div>
      <p id={descriptionId} className="sr-only">
        {description}
      </p>
    </div>
  );
}
