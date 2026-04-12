"use client";

import { useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import {
  FileSpreadsheet,
  LogOut,
  Menu,
  X,
  Cpu,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { clearToken } from "@/lib/auth";
import { Button } from "@/components/ui/button";

interface NavItem {
  label: string;
  href: string;
  icon: React.ElementType;
}

const navItems: NavItem[] = [
  { label: "Extraction", href: "/extraction", icon: FileSpreadsheet },
];

interface SidebarContentProps {
  pathname: string;
  onNavigate?: () => void;
  onLogout: () => void;
}

function SidebarContent({ pathname, onNavigate, onLogout }: SidebarContentProps) {
  const router = useRouter();

  function navigate(href: string) {
    router.push(href);
    onNavigate?.();
  }

  return (
    <div className="flex h-full flex-col">
      {/* Logo */}
      <div className="flex h-16 items-center gap-3 border-b border-slate-100 px-5">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-blue-600 shadow-sm">
          <Cpu className="h-4 w-4 text-white" />
        </div>
        <div className="flex flex-col">
          <span className="text-sm font-semibold leading-tight text-slate-900">
            SPIR Dynamic
          </span>
          <span className="text-[10px] leading-tight text-slate-400 tracking-wide uppercase">
            Extraction System
          </span>
        </div>
      </div>

      {/* Nav */}
      <nav className="flex-1 space-y-0.5 p-3">
        <p className="mb-2 px-2 text-[10px] font-semibold uppercase tracking-widest text-slate-400">
          Main
        </p>
        {navItems.map((item) => {
          const Icon = item.icon;
          const active = pathname === item.href || pathname.startsWith(item.href + "/");
          return (
            <button
              key={item.href}
              onClick={() => navigate(item.href)}
              className={cn(
                "flex w-full min-h-[44px] items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-all duration-150",
                active
                  ? "bg-blue-50 text-blue-700"
                  : "text-slate-600 hover:bg-slate-50 hover:text-slate-900"
              )}
            >
              <Icon
                className={cn(
                  "h-4 w-4 shrink-0",
                  active ? "text-blue-600" : "text-slate-400"
                )}
              />
              {item.label}
              {active && (
                <span className="ml-auto h-1.5 w-1.5 rounded-full bg-blue-600" />
              )}
            </button>
          );
        })}
      </nav>

      {/* Logout */}
      <div className="border-t border-slate-100 p-3">
        <button
          onClick={onLogout}
          className="flex w-full min-h-[44px] items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium text-slate-500 transition-all duration-150 hover:bg-red-50 hover:text-red-600"
        >
          <LogOut className="h-4 w-4 shrink-0" />
          Sign out
        </button>
      </div>
    </div>
  );
}

interface SidebarProps {
  children: React.ReactNode;
}

export function SidebarLayout({ children }: SidebarProps) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const router = useRouter();
  const pathname = usePathname();

  function handleLogout() {
    clearToken();
    router.push("/login");
  }

  return (
    <div className="flex h-screen overflow-hidden bg-background">
      {/* Desktop sidebar */}
      <aside className="hidden w-60 shrink-0 border-r border-slate-200 bg-white lg:flex lg:flex-col">
        <SidebarContent
          pathname={pathname}
          onLogout={handleLogout}
        />
      </aside>

      {/* Mobile overlay */}
      {mobileOpen && (
        <div
          className="fixed inset-0 z-40 bg-slate-900/50 backdrop-blur-sm lg:hidden"
          onClick={() => setMobileOpen(false)}
        />
      )}

      {/* Mobile drawer */}
      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-50 w-64 overflow-y-auto border-r border-slate-200 bg-white shadow-xl transition-transform duration-300 ease-in-out lg:hidden",
          mobileOpen ? "translate-x-0" : "-translate-x-full"
        )}
      >
        <button
          onClick={() => setMobileOpen(false)}
          className="absolute right-3 top-4 rounded-md p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
        >
          <X className="h-4 w-4" />
        </button>
        <SidebarContent
          pathname={pathname}
          onNavigate={() => setMobileOpen(false)}
          onLogout={handleLogout}
        />
      </aside>

      {/* Main content */}
      <div className="flex flex-1 flex-col overflow-hidden">
        {/* Mobile header */}
        <header className="flex h-14 items-center gap-3 border-b border-slate-200 bg-white px-4 lg:hidden">
          <button
            onClick={() => setMobileOpen(true)}
            className="rounded-md p-1.5 text-slate-500 hover:bg-slate-100 hover:text-slate-700"
          >
            <Menu className="h-5 w-5" />
          </button>
          <div className="flex items-center gap-2">
            <div className="flex h-6 w-6 items-center justify-center rounded-md bg-blue-600">
              <Cpu className="h-3.5 w-3.5 text-white" />
            </div>
            <span className="text-sm font-semibold text-slate-900">
              SPIR Dynamic
            </span>
          </div>
        </header>

        {/* Page content */}
        <main className="flex-1 overflow-y-auto">{children}</main>
      </div>
    </div>
  );
}
