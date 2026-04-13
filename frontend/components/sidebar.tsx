"use client";

import { useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import {
  FileSpreadsheet,
  LogOut,
  Menu,
  X,
  Gem,
  History,
  Settings,
  Bell,
  Search,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { clearToken } from "@/lib/auth";

interface NavItem {
  label: string;
  href: string;
  icon: React.ElementType;
}

const navItems: NavItem[] = [
  { label: "Extraction",  href: "/extraction",   icon: FileSpreadsheet },
  { label: "History",     href: "/history",      icon: History },
  { label: "Settings",    href: "/settings",     icon: Settings },
];

// ─── Sidebar Content ──────────────────────────────────────────────────────────

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
      <div className="flex h-16 items-center gap-3 border-b border-slate-100 px-4">
        <div className="flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-violet-600 to-purple-700 shadow-sm">
          <Gem className="h-4 w-4 text-white" />
        </div>
        <div className="flex flex-col flex-1 min-w-0">
          <span className="text-sm font-bold leading-tight text-slate-900 tracking-wide uppercase">
            SPIR ENTERPRISE
          </span>
          <span className="text-[10px] leading-tight text-slate-400 tracking-wide">
            Extraction Platform
          </span>
        </div>
        <span className="shrink-0 rounded-full bg-violet-100 px-2 py-0.5 text-[9px] font-bold uppercase tracking-wider text-violet-700">
          BETA
        </span>
      </div>

      {/* Nav */}
      <nav className="flex-1 space-y-0.5 p-3 overflow-y-auto">
        <p className="mb-2 px-2 text-[10px] font-semibold uppercase tracking-widest text-slate-400">
          Navigation
        </p>
        {navItems.map((item) => {
          const Icon = item.icon;
          const isActive =
            pathname === item.href ||
            pathname.startsWith(item.href + "/");

          return (
            <button
              key={item.label}
              onClick={() => navigate(item.href)}
              className={cn(
                "flex w-full min-h-[40px] items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-all duration-150",
                isActive
                  ? "bg-violet-50 text-violet-700"
                  : "text-slate-600 hover:bg-slate-50 hover:text-slate-900"
              )}
            >
              <Icon
                className={cn(
                  "h-4 w-4 shrink-0",
                  isActive ? "text-violet-600" : "text-slate-400"
                )}
              />
              {item.label}
              {isActive && (
                <span className="ml-auto h-1.5 w-1.5 rounded-full bg-violet-600" />
              )}
            </button>
          );
        })}
      </nav>

      {/* Logout */}
      <div className="border-t border-slate-100 p-3">
        <button
          onClick={onLogout}
          className="flex w-full min-h-[40px] items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium text-slate-500 transition-all duration-150 hover:bg-red-50 hover:text-red-600"
        >
          <LogOut className="h-4 w-4 shrink-0" />
          Sign out
        </button>
      </div>
    </div>
  );
}

// ─── Top Navbar ───────────────────────────────────────────────────────────────

function TopNavbar({ onMenuClick }: { onMenuClick?: () => void }) {
  return (
    <header className="flex h-14 items-center gap-3 border-b border-slate-200 bg-white px-4 lg:px-6">
      {/* Mobile menu */}
      {onMenuClick && (
        <button
          onClick={onMenuClick}
          className="rounded-md p-1.5 text-slate-500 hover:bg-slate-100 hover:text-slate-700 lg:hidden"
        >
          <Menu className="h-5 w-5" />
        </button>
      )}

      {/* Search */}
      <div className="relative flex-1 max-w-xs">
        <Search className="absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-400" />
        <input
          type="text"
          placeholder="Search data points..."
          className="h-9 w-full rounded-lg border border-slate-200 bg-slate-50 pl-8 pr-3 text-sm text-slate-700 placeholder:text-slate-400 focus:border-violet-400 focus:bg-white focus:outline-none focus:ring-2 focus:ring-violet-400/20 transition-colors"
        />
      </div>

      <div className="ml-auto flex items-center gap-3">
        {/* Notification bell */}
        <button className="relative rounded-lg p-2 text-slate-400 hover:bg-slate-100 hover:text-slate-600 transition-colors">
          <Bell className="h-4 w-4" />
          <span className="absolute right-1.5 top-1.5 h-1.5 w-1.5 rounded-full bg-violet-600" />
        </button>

        {/* Online badge */}
        <div className="flex items-center gap-1.5 rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1">
          <span className="h-2 w-2 rounded-full bg-emerald-500 animate-pulse" />
          <span className="text-xs font-medium text-emerald-700">Online</span>
        </div>

        {/* User avatar placeholder */}
        <div className="flex h-8 w-8 items-center justify-center rounded-full bg-violet-100 text-xs font-bold text-violet-700 cursor-pointer hover:bg-violet-200 transition-colors">
          SE
        </div>
      </div>
    </header>
  );
}

// ─── Layout ───────────────────────────────────────────────────────────────────

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
    <div className="flex h-screen overflow-hidden bg-slate-50">
      {/* Desktop sidebar */}
      <aside className="hidden w-60 shrink-0 border-r border-slate-200 bg-white lg:flex lg:flex-col">
        <SidebarContent pathname={pathname} onLogout={handleLogout} />
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
        <TopNavbar onMenuClick={() => setMobileOpen(true)} />
        <main className="flex-1 overflow-y-auto">{children}</main>
      </div>
    </div>
  );
}
