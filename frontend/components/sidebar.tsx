"use client";

import { useState, useEffect, useRef } from "react";
import { useRouter, usePathname } from "next/navigation";
import {
  FileSpreadsheet,
  LogOut,
  Menu,
  X,
  History,
  Settings,
  Moon,
  Sun,
  Search,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { clearToken, authHeaders } from "@/lib/auth";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

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
      <div className="flex h-16 items-center gap-3 border-b border-slate-100 dark:border-slate-700 px-4">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center overflow-hidden rounded-lg bg-white shadow-sm ring-1 ring-slate-100">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src="/cdc_logo.jpg"
            alt="CDC International"
            className="h-full w-full object-contain"
          />
        </div>
        <div className="flex flex-col flex-1 min-w-0">
          <span className="text-sm font-bold leading-tight text-slate-900 dark:text-white tracking-wide uppercase">
            SPIR TOOL
          </span>
          <span className="text-[10px] leading-tight text-slate-400 dark:text-slate-500 tracking-wide">
            Extraction Platform
          </span>
        </div>
      </div>

      {/* Nav */}
      <nav className="flex-1 space-y-0.5 p-3 overflow-y-auto">
        <p className="mb-2 px-2 text-[10px] font-semibold uppercase tracking-widest text-slate-400 dark:text-slate-500">
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
                  ? "bg-violet-50 text-violet-700 dark:bg-violet-950/50 dark:text-violet-400"
                  : "text-slate-600 hover:bg-slate-50 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-white"
              )}
            >
              <Icon
                className={cn(
                  "h-4 w-4 shrink-0",
                  isActive ? "text-violet-600 dark:text-violet-400" : "text-slate-400 dark:text-slate-500"
                )}
              />
              {item.label}
              {isActive && (
                <span className="ml-auto h-1.5 w-1.5 rounded-full bg-amber-500" />
              )}
            </button>
          );
        })}
      </nav>

      {/* Logout */}
      <div className="border-t border-slate-100 dark:border-slate-700 p-3">
        <button
          onClick={onLogout}
          className="flex w-full min-h-[40px] items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium text-slate-500 dark:text-slate-400 transition-all duration-150 hover:bg-red-50 hover:text-red-600 dark:hover:bg-red-950/30 dark:hover:text-red-400"
        >
          <LogOut className="h-4 w-4 shrink-0" />
          Sign out
        </button>
      </div>
    </div>
  );
}

// ─── Top Navbar ───────────────────────────────────────────────────────────────

function TopNavbar({
  onMenuClick,
  darkMode,
  onToggleDark,
  userInitials,
  username,
  count,
}: {
  onMenuClick?: () => void;
  darkMode: boolean;
  onToggleDark: () => void;
  userInitials: string;
  username: string;
  count: number;
}) {
  const [showProfile, setShowProfile] = useState(false);
  const profileRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (profileRef.current && !profileRef.current.contains(e.target as Node)) {
        setShowProfile(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  return (
    <header className="flex h-14 items-center gap-3 border-b border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900 px-4 lg:px-6">
      {/* Mobile menu */}
      {onMenuClick && (
        <button
          onClick={onMenuClick}
          className="rounded-md p-1.5 text-slate-500 hover:bg-slate-100 hover:text-slate-700 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-200 lg:hidden"
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
          className="h-9 w-full rounded-lg border border-slate-200 bg-slate-50 pl-8 pr-3 text-sm text-slate-700 placeholder:text-slate-400 focus:border-violet-400 focus:bg-white focus:outline-none focus:ring-2 focus:ring-violet-400/20 transition-colors dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200 dark:placeholder:text-slate-500 dark:focus:bg-slate-800"
        />
      </div>

      <div className="ml-auto flex items-center gap-3">
        {/* Dark mode toggle */}
        <button
          onClick={onToggleDark}
          className="rounded-lg p-2 text-slate-400 hover:bg-slate-100 hover:text-slate-600 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-200 transition-colors"
          title={darkMode ? "Switch to light mode" : "Switch to dark mode"}
        >
          {darkMode ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
        </button>

        {/* Online badge */}
        <div className="flex items-center gap-1.5 rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1 dark:border-emerald-800 dark:bg-emerald-950">
          <span className="h-2 w-2 rounded-full bg-emerald-500 animate-pulse" />
          <span className="text-xs font-medium text-emerald-700 dark:text-emerald-400">Online</span>
        </div>

        {/* User avatar + profile dropdown */}
        <div className="relative" ref={profileRef}>
          <button
            onClick={() => setShowProfile((p) => !p)}
            title={username}
            className="flex h-8 w-8 items-center justify-center rounded-full bg-violet-100 text-xs font-bold text-violet-700 cursor-pointer hover:bg-violet-200 dark:bg-violet-900 dark:text-violet-300 dark:hover:bg-violet-800 transition-colors"
          >
            {userInitials}
          </button>
          {showProfile && (
            <div className="absolute right-0 top-10 z-50 w-48 rounded-xl border border-slate-200 bg-white shadow-lg dark:border-slate-700 dark:bg-slate-800 p-3 space-y-1">
              <p className="text-xs font-semibold text-slate-700 dark:text-slate-200 truncate">{username || "—"}</p>
              <p className="text-xs text-slate-500 dark:text-slate-400">
                {count} file{count !== 1 ? "s" : ""} extracted
              </p>
            </div>
          )}
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
  const [darkMode, setDarkMode] = useState(false);
  const [userInitials, setUserInitials] = useState("??");
  const [username, setUsername] = useState("");
  const [count, setCount] = useState(0);
  const router = useRouter();
  const pathname = usePathname();

  // Load persisted theme on mount
  useEffect(() => {
    const saved = localStorage.getItem("theme");
    if (saved === "dark") setDarkMode(true);
  }, []);

  // Fetch current user profile and extraction count — force logout if token invalid/expired
  useEffect(() => {
    async function fetchMe() {
      try {
        const [meRes, histRes] = await Promise.all([
          fetch(`${API_URL}/api/me`, { headers: { ...authHeaders() } }),
          fetch(`${API_URL}/api/history`, { headers: { ...authHeaders() } }),
        ]);
        if (meRes.status === 401) {
          clearToken();
          window.location.href = "/login";
          return;
        }
        const data = await meRes.json();
        const name: string = data.username ?? "";
        setUsername(name);
        setUserInitials(name.slice(0, 2).toUpperCase() || "??");
        if (histRes.ok) {
          const hist = await histRes.json();
          setCount(Array.isArray(hist) ? hist.length : 0);
        }
      } catch {
        // network error — don't force logout
      }
    }
    fetchMe();
  }, []);

  // Persist theme and apply class to <html>
  useEffect(() => {
    localStorage.setItem("theme", darkMode ? "dark" : "light");
    document.documentElement.classList.toggle("dark", darkMode);
  }, [darkMode]);

  async function handleLogout() {
    try {
      await fetch(`${API_URL}/auth/logout`, {
        method: "POST",
        headers: { ...authHeaders() },
      });
    } catch {
      // ignore network errors — clear token regardless
    }
    clearToken();
    window.location.href = "/login";
  }

  return (
    <div className="flex h-screen overflow-hidden bg-slate-50 dark:bg-slate-950">
      {/* Desktop sidebar */}
      <aside className="hidden w-60 shrink-0 border-r border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900 lg:flex lg:flex-col">
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
          "fixed inset-y-0 left-0 z-50 w-64 overflow-y-auto border-r border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900 shadow-xl transition-transform duration-300 ease-in-out lg:hidden",
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
        <TopNavbar
          onMenuClick={() => setMobileOpen(true)}
          darkMode={darkMode}
          onToggleDark={() => setDarkMode((d) => !d)}
          userInitials={userInitials}
          username={username}
          count={count}
        />
        <main className="flex-1 overflow-y-auto">{children}</main>
      </div>
    </div>
  );
}
