import type { Metadata } from "next";
import { Plus_Jakarta_Sans } from "next/font/google";
import "./globals.css";
import { SidebarProvider } from "@/components/sidebar";
import { NavHistoryProvider } from "@/components/nav-history";

// Brand typeface (same as the login page).
const jakarta = Plus_Jakarta_Sans({ subsets: ["latin"], weight: ["400", "500", "600", "700"] });

export const metadata: Metadata = {
  // Browser-tab title: each route's layout.tsx sets its page name → "Spir | Extraction".
  title: { template: "Spir | %s", default: "Spir" },
  description: "Automated SPIR Excel extraction and processing",
  // Generated from public/cdc-logo.png (logo fills the square, transparent bg).
  icons: {
    icon: [
      { url: "/favicon.ico", sizes: "any" },
      { url: "/favicon-32x32.png", type: "image/png", sizes: "32x32" },
      { url: "/favicon-16x16.png", type: "image/png", sizes: "16x16" },
    ],
    apple: "/apple-touch-icon.png",
  },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        {/* Runs before first paint — marks html[data-admin] so CSS can reveal the
            Admin nav tab without waiting for React. Eliminates the insertion flash. */}
        <script
          dangerouslySetInnerHTML={{
            // Light theme only for now (dark mode removed); a stale saved 'theme' is ignored.
            __html: `try{var r=localStorage.getItem('role'),n=localStorage.getItem('profile_username')||'',i=n.slice(0,2).toUpperCase();if(['admin','super_admin','branch_admin'].indexOf(r)!==-1)document.documentElement.setAttribute('data-admin','1');if(i)document.documentElement.style.setProperty('--user-initials','"'+i+'"');if(localStorage.getItem('sidebar_collapsed')==='1')document.documentElement.setAttribute('data-sidebar','collapsed')}catch(e){}`,
          }}
        />
      </head>
      <body className={jakarta.className}>
        {/* Sidebar collapse state lives here (not in the per-page SidebarLayout)
            so it survives client-side navigation. */}
        <NavHistoryProvider>
          <SidebarProvider>{children}</SidebarProvider>
        </NavHistoryProvider>
      </body>
    </html>
  );
}