"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

// Lightweight accessible dropdown menu: click-outside + Escape close, arrow-key
// navigation between items, focus returns to the trigger on close.

interface MenuCtx {
  open: boolean;
  setOpen: (open: boolean) => void;
  triggerRef: React.RefObject<HTMLButtonElement>;
  contentRef: React.RefObject<HTMLDivElement>;
}
const Ctx = React.createContext<MenuCtx | null>(null);
const useMenu = () => {
  const ctx = React.useContext(Ctx);
  if (!ctx) throw new Error("DropdownMenu parts must be inside <DropdownMenu>");
  return ctx;
};

function DropdownMenu({
  children,
  onOpenChange,
}: {
  children: React.ReactNode;
  onOpenChange?: (open: boolean) => void;
}) {
  const [open, setOpenState] = React.useState(false);
  const triggerRef = React.useRef<HTMLButtonElement>(null);
  const contentRef = React.useRef<HTMLDivElement>(null);
  const rootRef = React.useRef<HTMLDivElement>(null);

  const setOpen = React.useCallback(
    (next: boolean) => {
      setOpenState(next);
      onOpenChange?.(next);
      if (!next) triggerRef.current?.focus();
    },
    [onOpenChange]
  );

  React.useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpenState(false);
        onOpenChange?.(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, setOpen, onOpenChange]);

  return (
    <Ctx.Provider value={{ open, setOpen, triggerRef, contentRef }}>
      <div ref={rootRef} className="relative">
        {children}
      </div>
    </Ctx.Provider>
  );
}

const DropdownMenuTrigger = React.forwardRef<HTMLButtonElement, React.ButtonHTMLAttributes<HTMLButtonElement>>(
  ({ onClick, ...props }, _ref) => {
    const { open, setOpen, triggerRef } = useMenu();
    return (
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={(e) => {
          onClick?.(e);
          setOpen(!open);
        }}
        {...props}
      />
    );
  }
);
DropdownMenuTrigger.displayName = "DropdownMenuTrigger";

function DropdownMenuContent({
  className,
  align = "end",
  children,
  ...props
}: React.HTMLAttributes<HTMLDivElement> & { align?: "start" | "end" }) {
  const { open, contentRef } = useMenu();

  const items = () =>
    Array.from(contentRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not([disabled])') ?? []);

  React.useEffect(() => {
    if (open) items()[0]?.focus();
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!open) return null;
  return (
    <div
      ref={contentRef}
      role="menu"
      onKeyDown={(e) => {
        if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
        e.preventDefault();
        const list = items();
        const i = list.indexOf(document.activeElement as HTMLElement);
        const next = e.key === "ArrowDown" ? (i + 1) % list.length : (i - 1 + list.length) % list.length;
        list[next]?.focus();
      }}
      className={cn(
        "absolute top-full z-50 mt-2 min-w-[14rem] overflow-hidden rounded-2xl border border-slate-200/80 bg-white p-1.5",
        "shadow-[0_24px_60px_-20px_rgba(15,23,42,0.25)] animate-in fade-in-0 zoom-in-95 duration-150",
        "dark:border-slate-800 dark:bg-slate-900",
        align === "end" ? "right-0 origin-top-right" : "left-0 origin-top-left",
        className
      )}
      {...props}
    >
      {children}
    </div>
  );
}

interface ItemProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  icon?: React.ElementType;
  /** Keep the menu open after selecting (e.g. toggles). */
  keepOpen?: boolean;
  tone?: "default" | "danger";
}

function DropdownMenuItem({ icon: Icon, keepOpen, tone = "default", className, onClick, children, ...props }: ItemProps) {
  const { setOpen } = useMenu();
  return (
    <button
      type="button"
      role="menuitem"
      onClick={(e) => {
        onClick?.(e);
        if (!keepOpen) setOpen(false);
      }}
      className={cn(
        "flex w-full items-center gap-2.5 rounded-xl px-3 py-2 text-left text-sm font-medium outline-none transition-colors",
        tone === "danger"
          ? "text-red-600 hover:bg-red-50 focus-visible:bg-red-50 dark:text-red-400 dark:hover:bg-red-950/40 dark:focus-visible:bg-red-950/40"
          : "text-slate-700 hover:bg-slate-100 focus-visible:bg-slate-100 dark:text-slate-200 dark:hover:bg-slate-800 dark:focus-visible:bg-slate-800",
        className
      )}
      {...props}
    >
      {Icon && <Icon className={cn("h-4 w-4 shrink-0", tone === "danger" ? "" : "text-slate-400 dark:text-slate-500")} />}
      {children}
    </button>
  );
}

function DropdownMenuSeparator({ className }: { className?: string }) {
  return <div role="separator" className={cn("my-1.5 h-px bg-slate-100 dark:bg-slate-800", className)} />;
}

export { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator };
