import * as React from "react";
import { cn } from "@/lib/utils";

// Soft-fill field with a navy focus glow (matches the login page).
export const inputClassName =
  "flex h-10 w-full rounded-xl border border-slate-200/80 bg-slate-50 px-3.5 text-sm text-slate-900 transition-all " +
  "placeholder:text-slate-400 focus:border-brand-700/40 focus:bg-white focus:outline-none focus:ring-4 focus:ring-brand-700/10 " +
  "disabled:cursor-not-allowed disabled:opacity-60 " +
  "dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 dark:placeholder:text-slate-500 dark:focus:bg-slate-950 dark:focus:ring-brand-400/20";

export type InputProps = React.InputHTMLAttributes<HTMLInputElement>;

const Input = React.forwardRef<HTMLInputElement, InputProps>(({ className, ...props }, ref) => (
  <input className={cn(inputClassName, className)} ref={ref} {...props} />
));
Input.displayName = "Input";

export { Input };
