import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

// Capsule badges: soft pastel fill, high-contrast text.
const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-[11px] font-semibold leading-4",
  {
    variants: {
      variant: {
        brand: "bg-brand-50 text-brand-700 dark:bg-brand-950 dark:text-brand-300",
        gold: "bg-cdc-gold/20 text-amber-800 dark:bg-cdc-gold/15 dark:text-cdc-gold",
        slate: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300",
        green: "bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300",
        amber: "bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
        red: "bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300",
        blue: "bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300",
        outline: "border border-slate-200 text-slate-600 dark:border-slate-700 dark:text-slate-300",
      },
    },
    defaultVariants: { variant: "brand" },
  }
);

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
