import * as React from "react";
import { cn } from "@/lib/utils";

const Label = React.forwardRef<HTMLLabelElement, React.LabelHTMLAttributes<HTMLLabelElement>>(
  ({ className, ...props }, ref) => (
    <label
      ref={ref}
      className={cn("mb-1.5 block text-xs font-semibold text-slate-700 dark:text-slate-300", className)}
      {...props}
    />
  )
);
Label.displayName = "Label";

/** Small helper text under a field. */
function FieldHint({ className, ...props }: React.HTMLAttributes<HTMLParagraphElement>) {
  return <p className={cn("mt-1.5 text-[11px] text-slate-500 dark:text-slate-400", className)} {...props} />;
}

export { Label, FieldHint };
