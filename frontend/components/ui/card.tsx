import * as React from "react";
import { cn } from "@/lib/utils";

// Elevated surface: deep radius, ghost border, soft ambient shadow.
const Card = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(({ className, ...props }, ref) => (
  <div
    ref={ref}
    className={cn(
      "overflow-hidden rounded-3xl border border-slate-200/80 bg-white shadow-[0_20px_50px_-24px_rgba(15,23,42,0.12)]",
      "dark:border-slate-800 dark:bg-slate-900",
      className
    )}
    {...props}
  />
));
Card.displayName = "Card";

interface CardHeaderProps extends React.HTMLAttributes<HTMLDivElement> {
  icon?: React.ElementType;
  /** Right-aligned slot (actions, badges). */
  action?: React.ReactNode;
}

function CardHeader({ className, icon: Icon, action, children, ...props }: CardHeaderProps) {
  return (
    <div
      className={cn("flex items-center gap-3 border-b border-slate-100 px-6 py-4 dark:border-slate-800", className)}
      {...props}
    >
      {Icon && (
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-brand-50 text-brand-700 dark:bg-brand-950 dark:text-brand-300">
          <Icon className="h-4 w-4" />
        </span>
      )}
      <div className="min-w-0 flex-1">{children}</div>
      {action}
    </div>
  );
}

function CardTitle({ className, ...props }: React.HTMLAttributes<HTMLHeadingElement>) {
  return <h2 className={cn("text-sm font-bold text-slate-900 dark:text-slate-100", className)} {...props} />;
}

function CardDescription({ className, ...props }: React.HTMLAttributes<HTMLParagraphElement>) {
  return <p className={cn("mt-0.5 text-xs text-slate-500 dark:text-slate-400", className)} {...props} />;
}

function CardContent({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("p-6", className)} {...props} />;
}

function CardFooter({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "flex items-center justify-end gap-2 border-t border-slate-100 bg-slate-50/60 px-6 py-3 dark:border-slate-800 dark:bg-slate-900/60",
        className
      )}
      {...props}
    />
  );
}

export { Card, CardHeader, CardTitle, CardDescription, CardContent, CardFooter };
