import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import { SidebarProvider } from "@/components/sidebar";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "SPIR TOOL",
  description: "Automated SPIR Excel extraction and processing",
  icons: {
    icon: "/favicon.ico",
    shortcut: "/favicon-16x16.png",
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
            __html: `try{var t=localStorage.getItem('theme');if(t==='dark')document.documentElement.classList.add('dark');var r=localStorage.getItem('role'),n=localStorage.getItem('profile_username')||'',i=n.slice(0,2).toUpperCase();if(['admin','super_admin','branch_admin'].indexOf(r)!==-1)document.documentElement.setAttribute('data-admin','1');if(i)document.documentElement.style.setProperty('--user-initials','"'+i+'"');if(localStorage.getItem('sidebar_collapsed')==='1')document.documentElement.setAttribute('data-sidebar','collapsed')}catch(e){}`,
          }}
        />
      </head>
      <body className={inter.className}>
        {/* Sidebar collapse state lives here (not in the per-page SidebarLayout)
            so it survives client-side navigation. */}
        <SidebarProvider>{children}</SidebarProvider>
      </body>
    </html>
  );
}