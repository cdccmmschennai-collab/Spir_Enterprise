import type { Metadata } from "next";

// Browser-tab title for this route; the root layout prefixes "Spir | ".
export const metadata: Metadata = { title: "Guide" };

export default function Layout({ children }: { children: React.ReactNode }) {
  return children;
}
