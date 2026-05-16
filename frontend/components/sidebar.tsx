"use client";

import { useState, useEffect, useLayoutEffect, useRef, useCallback, memo } from "react";
import { useRouter, usePathname } from "next/navigation";
import {
  FileSpreadsheet,
  LogOut,
  Menu,
  X,
  History,
  Settings,
  Search,
  ShieldCheck,
  User,
  Pencil,
  ImagePlus,
  ImageOff,
  RefreshCw,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { clearToken, authHeaders, getRole } from "@/lib/auth";
import { clearSession } from "@/lib/extraction-session";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

let lastProfileFetchAt = 0;
const PROFILE_STALE_MS = 2 * 60 * 1000;

const useIsomorphicLayoutEffect = typeof window !== "undefined" ? useLayoutEffect : useEffect;

interface NavItem {
  label: string;
  href: string;
  icon: React.ElementType;
}

const baseNavItems: NavItem[] = [
  { label: "Extraction",  href: "/extraction",   icon: FileSpreadsheet },
  { label: "History",     href: "/history",      icon: History },
  { label: "Settings",    href: "/settings",     icon: Settings },
];

// ─── Sidebar Content ──────────────────────────────────────────────────────────

interface SidebarContentProps {
  pathname: string;
  onNavigate?: () => void;
  isAdmin?: boolean;
}

const SidebarContent = memo(function SidebarContent({ pathname, onNavigate, isAdmin }: SidebarContentProps) {
  const router = useRouter();
  const adminIsActive = pathname === "/admin" || pathname.startsWith("/admin/");

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
        {baseNavItems.map((item) => {
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

        <button
          onClick={() => navigate("/admin")}
          aria-hidden={!isAdmin}
          tabIndex={isAdmin ? 0 : -1}
          className={cn(
            "admin-nav-item flex w-full min-h-[40px] items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-all duration-150",
            adminIsActive
              ? "bg-violet-50 text-violet-700 dark:bg-violet-950/50 dark:text-violet-400"
              : "text-slate-600 hover:bg-slate-50 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-white"
          )}
        >
          <ShieldCheck
            className={cn(
              "h-4 w-4 shrink-0",
              adminIsActive ? "text-violet-600 dark:text-violet-400" : "text-slate-400 dark:text-slate-500"
            )}
          />
          Admin
          {adminIsActive && (
            <span className="ml-auto h-1.5 w-1.5 rounded-full bg-amber-500" />
          )}
        </button>
      </nav>
    </div>
  );
});

// ─── Top Navbar ───────────────────────────────────────────────────────────────

const TopNavbar = memo(function TopNavbar({
  onMenuClick,
  userInitials,
  username,
  email,
  role,
  count,
  avatarUrl,
  onLogout,
  onAvatarUploaded,
  onAvatarRemoved,
}: {
  onMenuClick?: () => void;
  userInitials: string;
  username: string;
  email: string;
  role: string;
  count: number;
  avatarUrl: string;
  onLogout: () => void;
  onAvatarUploaded: (url: string) => void;
  onAvatarRemoved: () => void;
}) {
  const [showProfile, setShowProfile] = useState(false);
  const [showAvatarMenu, setShowAvatarMenu] = useState(false);
  const [uploading, setUploading] = useState(false);
  const profileRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (profileRef.current && !profileRef.current.contains(e.target as Node)) {
        setShowProfile(false);
      }
    }
    function handleEscape(e: KeyboardEvent) {
      if (e.key === "Escape") setShowProfile(false);
    }
    document.addEventListener("mousedown", handleClickOutside);
    document.addEventListener("keydown", handleEscape);
    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
      document.removeEventListener("keydown", handleEscape);
    };
  }, []);

  useEffect(() => {
    if (!showProfile) setShowAvatarMenu(false);
  }, [showProfile]);

  async function handleAvatarUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      const form = new FormData();
      form.append("file", file);
      const res = await fetch(`${API_URL}/api/avatar`, {
        method: "POST",
        headers: { ...authHeaders() },
        body: form,
      });
      if (res.ok) {
        const data = await res.json();
        onAvatarUploaded(data.avatar_url);
      }
    } catch {
      // ignore
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  async function handleAvatarRemove() {
    setShowAvatarMenu(false);
    setUploading(true);
    try {
      const res = await fetch(`${API_URL}/api/avatar`, {
        method: "DELETE",
        headers: { ...authHeaders() },
      });
      if (res.ok) {
        onAvatarRemoved();
      }
    } catch {
      // ignore
    } finally {
      setUploading(false);
    }
  }

  const avatarSrc = avatarUrl ? `${API_URL}${avatarUrl}` : "";

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

      <div className="ml-auto flex items-center">
        {/* User avatar + profile dropdown */}
        <div className="relative" ref={profileRef}>
          <button
            onClick={() => {
              window.dispatchEvent(new CustomEvent("profile-refresh"));
              setShowProfile((p) => !p);
            }}
            title={username}
            aria-label="Open profile menu"
            aria-expanded={showProfile}
            className="avatar-btn flex h-8 w-8 shrink-0 items-center justify-center rounded-full overflow-hidden bg-violet-600 text-xs font-bold text-white ring-2 ring-violet-300 ring-offset-1 dark:ring-violet-700 dark:ring-offset-slate-900 cursor-pointer hover:ring-violet-400 transition-all duration-150 shadow-sm"
          >
            {avatarSrc ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={avatarSrc} alt={username} className="h-full w-full object-cover" />
            ) : (
              userInitials || <User className="h-4 w-4" />
            )}
          </button>

          {showProfile && (
            <div
              role="menu"
              className="absolute right-0 top-10 z-50 w-64 rounded-2xl border border-slate-200 bg-white shadow-2xl dark:border-slate-700 dark:bg-slate-900 overflow-hidden animate-in fade-in-0 zoom-in-95 duration-150"
            >
              {/* Hidden file input */}
              <input
                ref={fileInputRef}
                type="file"
                accept="image/jpeg,image/png,image/webp,image/gif"
                className="hidden"
                onChange={handleAvatarUpload}
              />

              {/* Identity block */}
              <div className="flex items-center gap-3 px-4 pt-4 pb-3">
                <div className="relative shrink-0">
                  <div className="flex h-12 w-12 items-center justify-center rounded-xl overflow-hidden bg-violet-100 dark:bg-violet-900/60 shadow-sm ring-1 ring-violet-200 dark:ring-violet-800">
                    {avatarSrc ? (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img src={avatarSrc} alt={username} className="h-full w-full object-cover" />
                    ) : (
                      <span className="text-base font-bold text-violet-700 dark:text-violet-300">
                        {userInitials || <User className="h-5 w-5" />}
                      </span>
                    )}
                  </div>
                  <button
                    onClick={() => setShowAvatarMenu((p) => !p)}
                    disabled={uploading}
                    title="Edit photo"
                    className="absolute -bottom-1 -right-1 flex h-5 w-5 items-center justify-center rounded-full bg-violet-600 text-white hover:bg-violet-700 transition-colors shadow-sm disabled:opacity-60"
                  >
                    <Pencil className="h-2.5 w-2.5" />
                  </button>
                  {showAvatarMenu && (
                    <div className="absolute top-full left-0 mt-1.5 z-[70] min-w-[168px] rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 shadow-xl overflow-hidden">
                      {avatarSrc ? (
                        <>
                          <button
                            onClick={() => { setShowAvatarMenu(false); fileInputRef.current?.click(); }}
                            className="flex w-full items-center gap-2 px-3 py-2 text-xs text-slate-700 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800 transition-colors"
                          >
                            <RefreshCw className="h-3 w-3 shrink-0 text-slate-400" />
                            Change Profile Photo
                          </button>
                          <button
                            onClick={handleAvatarRemove}
                            className="flex w-full items-center gap-2 px-3 py-2 text-xs text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-950/30 transition-colors"
                          >
                            <ImageOff className="h-3 w-3 shrink-0" />
                            Remove Profile Photo
                          </button>
                        </>
                      ) : (
                        <button
                          onClick={() => { setShowAvatarMenu(false); fileInputRef.current?.click(); }}
                          className="flex w-full items-center gap-2 px-3 py-2 text-xs text-slate-700 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800 transition-colors"
                        >
                          <ImagePlus className="h-3 w-3 shrink-0 text-slate-400" />
                          Upload Profile Photo
                        </button>
                      )}
                    </div>
                  )}
                </div>

                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5 mb-0.5">
                    <p className="truncate text-sm font-bold text-slate-900 dark:text-slate-100 leading-tight">
                      {username || "—"}
                    </p>
                    {role && (
                      <span className={cn(
                        "shrink-0 rounded-full px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide",
                        role === "admin"
                          ? "bg-violet-100 text-violet-700 dark:bg-violet-950 dark:text-violet-300"
                          : "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400"
                      )}>
                        {role}
                      </span>
                    )}
                  </div>
                  {email && (
                    <p className="truncate text-xs text-slate-500 dark:text-slate-400">{email}</p>
                  )}
                </div>
              </div>

              {/* Extractions count */}
              <div className="mx-4 mb-3 flex items-center justify-between rounded-lg bg-slate-50 dark:bg-slate-800/60 px-3 py-2">
                <span className="text-xs font-medium text-slate-600 dark:text-slate-300">
                  Total Extractions
                </span>
                <span className="text-base font-bold text-violet-700 dark:text-violet-400 tabular-nums">
                  {count.toLocaleString()}
                </span>
              </div>

              {/* Sign out */}
              <div className="px-4 pb-4">
                <button
                  role="menuitem"
                  onClick={() => { setShowProfile(false); onLogout(); }}
                  className="flex w-full items-center justify-center gap-2 rounded-xl bg-slate-900 px-4 py-2.5 text-sm font-semibold text-white hover:bg-slate-700 dark:bg-violet-900 dark:hover:bg-violet-800 transition-all duration-150 active:scale-[0.98]"
                >
                  <LogOut className="h-4 w-4" />
                  Sign Out
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </header>
  );
});

// ─── Layout ───────────────────────────────────────────────────────────────────

interface SidebarProps {
  children: React.ReactNode;
}

export function SidebarLayout({ children }: SidebarProps) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const [darkMode, setDarkMode] = useState(false);
  const [mounted, setMounted] = useState(false);
  const [userInitials, setUserInitials] = useState("");
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("");
  const [count, setCount] = useState(0);
  const [avatarUrl, setAvatarUrl] = useState("");
  const pathname = usePathname();

  const [isAdmin, setIsAdmin] = useState(false);

  useIsomorphicLayoutEffect(() => {
    const saved = localStorage.getItem("theme");
    if (saved === "dark") setDarkMode(true);

    const cachedName = localStorage.getItem("profile_username") ?? "";
    if (cachedName) {
      setUsername(cachedName);
      setUserInitials(cachedName.slice(0, 2).toUpperCase() || "");
    }

    const cachedAvatar = localStorage.getItem("profile_avatar_url") ?? "";
    if (cachedAvatar) setAvatarUrl(cachedAvatar);

    const admin = getRole() === "admin";
    setIsAdmin(admin);
    if (admin) {
      document.documentElement.setAttribute("data-admin", "1");
    } else {
      document.documentElement.removeAttribute("data-admin");
    }

    document.documentElement.setAttribute("data-ready", "1");
    setMounted(true);
  }, []);

  const refreshProfile = useCallback(async () => {
    lastProfileFetchAt = Date.now();
    try {
      const meRes = await fetch(`${API_URL}/api/me`, { headers: { ...authHeaders() } });
      if (meRes.status === 401) {
        clearToken();
        window.location.href = "/login";
        return;
      }
      const data = await meRes.json();
      const name: string = data.username ?? "";
      setUsername(name);
      setUserInitials(name.slice(0, 2).toUpperCase() || "");
      setEmail(data.email ?? "");
      setRole(data.role ?? "");
      setCount(data.total_files_extracted ?? 0);
      const av: string = data.avatar_url ?? "";
      setAvatarUrl(av);
      if (name) localStorage.setItem("profile_username", name);
      if (av) localStorage.setItem("profile_avatar_url", av);
      else localStorage.removeItem("profile_avatar_url");
    } catch {
      // network error — don't force logout
    }
  }, []);

  useEffect(() => {
    if (Date.now() - lastProfileFetchAt >= PROFILE_STALE_MS) {
      refreshProfile();
    }
    window.addEventListener("profile-refresh", refreshProfile);
    return () => window.removeEventListener("profile-refresh", refreshProfile);
  }, [refreshProfile]);

  // Persist theme and apply class to <html>
  useEffect(() => {
    localStorage.setItem("theme", darkMode ? "dark" : "light");
    document.documentElement.classList.toggle("dark", darkMode);
  }, [darkMode]);

  const handleLogout = useCallback(async () => {
    try {
      await fetch(`${API_URL}/auth/logout`, {
        method: "POST",
        headers: { ...authHeaders() },
      });
    } catch {
      // ignore
    }
    clearSession();
    clearToken();
    localStorage.removeItem("profile_avatar_url");
    window.location.href = "/login";
  }, []);

  const handleAvatarUploaded = useCallback((url: string) => {
    setAvatarUrl(url);
    localStorage.setItem("profile_avatar_url", url);
  }, []);

  const handleAvatarRemoved = useCallback(() => {
    setAvatarUrl("");
    localStorage.removeItem("profile_avatar_url");
  }, []);

  const handleMenuClick = useCallback(() => setMobileOpen(true), []);
  const handleCloseMobile = useCallback(() => setMobileOpen(false), []);

  return (
    <div className="flex h-screen overflow-hidden bg-slate-50 dark:bg-slate-950">
      {/* Desktop sidebar */}
      <aside className="hidden w-60 shrink-0 border-r border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900 lg:flex lg:flex-col">
        <SidebarContent pathname={pathname} isAdmin={mounted && isAdmin} />
      </aside>

      {/* Mobile overlay */}
      {mobileOpen && (
        <div
          className="fixed inset-0 z-40 bg-slate-900/50 backdrop-blur-sm lg:hidden"
          onClick={handleCloseMobile}
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
          onClick={handleCloseMobile}
          className="absolute right-3 top-4 rounded-md p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
        >
          <X className="h-4 w-4" />
        </button>
        <SidebarContent
          pathname={pathname}
          onNavigate={handleCloseMobile}
          isAdmin={mounted && isAdmin}
        />
      </aside>

      {/* Main content */}
      <div className="flex flex-1 flex-col overflow-hidden">
        <TopNavbar
          onMenuClick={handleMenuClick}
          userInitials={userInitials}
          username={username}
          email={email}
          role={role}
          count={count}
          avatarUrl={avatarUrl}
          onLogout={handleLogout}
          onAvatarUploaded={handleAvatarUploaded}
          onAvatarRemoved={handleAvatarRemoved}
        />
        <main className="flex-1 overflow-y-auto">{children}</main>
      </div>
    </div>
  );
}
