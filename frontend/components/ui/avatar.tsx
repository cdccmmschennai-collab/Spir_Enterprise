import * as React from "react";
import { User } from "lucide-react";
import { cn } from "@/lib/utils";

interface AvatarProps extends React.HTMLAttributes<HTMLSpanElement> {
  src?: string;
  name?: string;
  size?: "sm" | "md" | "lg" | "xl";
  /** "solid" = navy fill with white initials; "soft" = tinted fill. */
  tone?: "solid" | "soft";
}

const sizes = {
  sm: "h-8 w-8 text-xs",
  md: "h-10 w-10 text-sm",
  lg: "h-12 w-12 text-base",
  xl: "h-16 w-16 text-xl",
};

export function initialsOf(name?: string) {
  return (name ?? "").trim().slice(0, 2).toUpperCase();
}

/** Round avatar: photo when available, otherwise initials (or a user icon). */
function Avatar({ src, name, size = "md", tone = "soft", className, ...props }: AvatarProps) {
  const initials = initialsOf(name);
  return (
    <span
      className={cn(
        "relative inline-flex shrink-0 items-center justify-center overflow-hidden rounded-full font-bold",
        tone === "solid"
          ? "bg-brand-700 text-white"
          : "bg-brand-50 text-brand-700 ring-1 ring-brand-100 dark:bg-brand-950 dark:text-brand-300 dark:ring-brand-900",
        sizes[size],
        className
      )}
      {...props}
    >
      {src ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={src} alt={name ?? ""} className="h-full w-full object-cover" />
      ) : (
        initials || <User className="h-1/2 w-1/2" />
      )}
    </span>
  );
}

export { Avatar };
