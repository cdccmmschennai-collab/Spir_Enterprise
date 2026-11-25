import type { BadgeProps } from "@/components/ui/badge";

/** Display label + badge colour for a user role (one place, used app-wide). */
export function roleMeta(role?: string | null): { label: string; variant: NonNullable<BadgeProps["variant"]> } {
  switch (role) {
    case "super_admin":
    case "admin": // legacy tokens
      return { label: "Super Admin", variant: "gold" };
    case "branch_admin":
      return { label: "Branch Admin", variant: "brand" };
    default:
      return { label: role ? role.replace(/_/g, " ") : "", variant: "slate" };
  }
}
