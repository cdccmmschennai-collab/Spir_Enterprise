import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";

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
            __html: `try{var r=localStorage.getItem('role'),n=localStorage.getItem('profile_username')||'',i=n.slice(0,2).toUpperCase();if(r==='admin')document.documentElement.setAttribute('data-admin','1');if(i)document.documentElement.style.setProperty('--user-initials','"'+i+'"')}catch(e){}`,
          }}
        />
      </head>
      <body className={inter.className}>{children}</body>
    </html>
  );
}