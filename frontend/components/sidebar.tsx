"use client";

import { useState, useEffect, useLayoutEffect, useRef, useCallback, useContext, createContext, memo } from "react";
import { useRouter, usePathname } from "next/navigation";
import {
  FileSpreadsheet,
  GitCompare,
  Layers,
  LogOut,
  Menu,
  X,
  History,
  Settings,
  PanelLeftClose,
  PanelLeftOpen,
  ShieldCheck,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { clearToken, authHeaders, getRole } from "@/lib/auth";
import { clearSession } from "@/lib/extraction-session";
import { clearCompareSession, resetCompareMemory } from "@/lib/compare-session";
import { roleMeta } from "@/lib/roles";
import { Avatar } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

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
  { label: "Batch Extraction", href: "/batch",     icon: Layers },
  { label: "SPIR Comparison", href: "/compare",    icon: GitCompare },
  { label: "History",     href: "/history",      icon: History },
  // Settings lives in the profile menu (top-right avatar), not the sidebar.
];

// ─── Sidebar collapse state ───────────────────────────────────────────────────
//
// Every page renders its own <SidebarLayout>, so that component unmounts and
// remounts on each route change. State kept inside it would reset on every
// navigation. The collapse state therefore lives in this provider, mounted
// once in app/layout.tsx — the only tree node that survives navigation.

const SIDEBAR_COLLAPSED_KEY = "sidebar_collapsed";

interface SidebarState {
  collapsed: boolean;
  toggle: () => void;
  // False until after the first paint; the width transition is only enabled
  // once true, so restoring a saved "collapsed" on page load never animates.
  animate: boolean;
}

const SidebarContext = createContext<SidebarState>({ collapsed: false, toggle: () => {}, animate: false });

export function SidebarProvider({ children }: { children: React.ReactNode }) {
  const [collapsed, setCollapsed] = useState(false);
  const [animate, setAnimate] = useState(false);

  // Layout effect: restored before React's first paint. The prerendered HTML
  // was already forced into the rail by the inline script in app/layout.tsx
  // (html[data-sidebar] + globals.css); drop that hook now that state owns it.
  useIsomorphicLayoutEffect(() => {
    try {
      if (localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1") setCollapsed(true);
    } catch {
      // storage unavailable — stay expanded
    }
    document.documentElement.removeAttribute("data-sidebar");
  }, []);

  // Passive effect: runs after that first paint.
  useEffect(() => setAnimate(true), []);

  const toggle = useCallback(() => {
    setCollapsed((c) => {
      const next = !c;
      try {
        localStorage.setItem(SIDEBAR_COLLAPSED_KEY, next ? "1" : "0");
      } catch {
        // storage unavailable — in-memory state still persists across routes
      }
      return next;
    });
  }, []);

  return (
    <SidebarContext.Provider value={{ collapsed, toggle, animate }}>
      {children}
    </SidebarContext.Provider>
  );
}

// ─── Sidebar Content ──────────────────────────────────────────────────────────

interface SidebarContentProps {
  pathname: string;
  onNavigate?: () => void;
  isAdmin?: boolean;
  // Icon-only rail (desktop). Labels stay in the DOM and fade/shrink so the
  // width transition is smooth; `title` carries the label as a tooltip.
  collapsed?: boolean;
  // Header toggle; omitted in the mobile drawer (it has its own close button).
  onToggle?: () => void;
}

// Labels and the brand block share one fade + shrink so nothing wraps mid-transition.
const LABEL_TRANSITION = "overflow-hidden whitespace-nowrap transition-[max-width,opacity] duration-200 ease-in-out";
const LABEL_OPEN = "max-w-[160px] opacity-100";
const LABEL_CLOSED = "max-w-0 opacity-0";

const SidebarContent = memo(function SidebarContent({ pathname, onNavigate, isAdmin, collapsed = false, onToggle }: SidebarContentProps) {
  const router = useRouter();
  const adminIsActive = pathname === "/admin" || pathname.startsWith("/admin/");

  function navigate(href: string) {
    router.push(href);
    onNavigate?.();
  }

  const navButtonClass = (active: boolean) =>
    cn(
      "sb-row flex w-full min-h-[40px] items-center rounded-lg py-2 text-sm font-medium transition-all duration-200",
      collapsed ? "justify-center gap-0 px-0" : "gap-3 px-3",
      active
        ? "bg-brand-50 text-brand-700 dark:bg-brand-950/50 dark:text-brand-400"
        : "text-slate-600 hover:bg-slate-50 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-white"
    );

  // Collapsed rail: only the logo shows. Hovering the header (or tapping it on
  // touch, or keyboard-focusing the button) crossfades the logo out and the
  // expand icon in, in the same spot. Hover is state-driven rather than CSS
  // :hover so that right after clicking "collapse" — with the pointer still
  // over the header — the logo shows; hover re-arms once the pointer leaves.
  const [revealed, setRevealed] = useState(false);
  const [hoverArmed, setHoverArmed] = useState(true);
  useEffect(() => setRevealed(false), [collapsed]);

  const headerEvents = collapsed
    ? {
        onMouseEnter: () => { if (hoverArmed) setRevealed(true); },
        onMouseLeave: () => { setRevealed(false); setHoverArmed(true); },
        onPointerDown: (e: React.PointerEvent) => { if (e.pointerType !== "mouse") setRevealed(true); },
      }
    : undefined;

  return (
    <div className="flex h-full flex-col">
      {/* Header: logo + brand + icon-only toggle. Collapsed: the toggle is
          absolutely centered over the logo, so revealing it changes no
          dimensions — the h-16 border stays level with the navbar. */}
      <div
        className={cn(
          "sb-row group relative flex h-16 shrink-0 items-center border-b border-slate-100 dark:border-slate-700",
          // pl-[18px]: centres the 28px logo on the nav icons' column (x = 32px).
          collapsed ? "justify-center" : "gap-3 pl-[18px] pr-4"
        )}
        {...headerEvents}
      >
        <div
          className={cn(
            // Icon-sized mark (24px tall, ~1.16:1 aspect) to match the nav icons.
            "flex h-6 w-7 shrink-0 items-center justify-center",
            collapsed && cn(
              // Keyboard focus only (focus-visible): a mouse click that collapses
              // the sidebar leaves the button focused, but must not hide the logo.
              "transition-opacity duration-150 ease-out motion-reduce:transition-none group-has-[:focus-visible]:opacity-0",
              revealed && "opacity-0"
            )
          )}
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src="/cdc-logo.png"
            alt="CDC — Construction Development Company"
            className="h-full w-full object-contain"
          />
        </div>
        {!collapsed && (
          // Single line, vertically centred on the logo (the header row is items-center).
          <span className="sb-expanded-only min-w-0 flex-1 truncate text-[15px] font-bold leading-6 tracking-[-0.01em] text-slate-900">
            Spir Tool
          </span>
        )}
        {onToggle && (
          <button
            type="button"
            onClick={() => {
              // Pointer is still over the header after this click; don't let
              // that count as a hover until it has left once.
              setHoverArmed(false);
              setRevealed(false);
              onToggle();
            }}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-expanded={!collapsed}
            aria-controls="app-sidebar"
            title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            className={cn(
              "flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-slate-400 hover:text-brand-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400/40 dark:text-slate-500 dark:hover:text-brand-300",
              collapsed
                ? cn(
                    // Stacked exactly over the centered 32px logo; hidden until revealed.
                    "absolute left-1/2 top-1/2 h-8 w-8 -translate-x-1/2 -translate-y-1/2",
                    "transition-opacity duration-150 ease-out motion-reduce:transition-none",
                    "focus-visible:pointer-events-auto focus-visible:opacity-100",
                    revealed ? "pointer-events-auto opacity-100" : "pointer-events-none opacity-0"
                  )
                : "sb-expanded-only transition-colors"
            )}
          >
            {collapsed ? <PanelLeftOpen className="h-5 w-5" /> : <PanelLeftClose className="h-5 w-5" />}
          </button>
        )}
      </div>

      {/* Nav */}
      <nav className="flex-1 space-y-0.5 p-3 overflow-y-auto overflow-x-hidden">
        {baseNavItems.map((item) => {
          const Icon = item.icon;
          const isActive =
            pathname === item.href ||
            pathname.startsWith(item.href + "/");

          return (
            <button
              key={item.label}
              onClick={() => navigate(item.href)}
              aria-label={item.label}
              aria-current={isActive ? "page" : undefined}
              title={collapsed ? item.label : undefined}
              className={navButtonClass(isActive)}
            >
              <Icon
                className={cn(
                  "h-4 w-4 shrink-0",
                  isActive ? "text-brand-600 dark:text-brand-400" : "text-slate-400 dark:text-slate-500"
                )}
              />
              <span className={cn("sb-expanded-only", LABEL_TRANSITION, collapsed ? LABEL_CLOSED : LABEL_OPEN)}>{item.label}</span>
              {isActive && !collapsed && (
                <span className="sb-expanded-only ml-auto h-1.5 w-1.5 rounded-full bg-cdc-gold" />
              )}
            </button>
          );
        })}

        <button
          onClick={() => navigate("/admin")}
          aria-hidden={!isAdmin}
          tabIndex={isAdmin ? 0 : -1}
          aria-label="Admin"
          aria-current={adminIsActive ? "page" : undefined}
          title={collapsed ? "Admin" : undefined}
          className={cn("admin-nav-item", navButtonClass(adminIsActive))}
        >
          <ShieldCheck
            className={cn(
              "h-4 w-4 shrink-0",
              adminIsActive ? "text-brand-600 dark:text-brand-400" : "text-slate-400 dark:text-slate-500"
            )}
          />
          <span className={cn("sb-expanded-only", LABEL_TRANSITION, collapsed ? LABEL_CLOSED : LABEL_OPEN)}>Admin</span>
          {adminIsActive && !collapsed && (
            <span className="sb-expanded-only ml-auto h-1.5 w-1.5 rounded-full bg-cdc-gold" />
          )}
        </button>
      </nav>
    </div>
  );
});

// ─── Top Navbar ───────────────────────────────────────────────────────────────

const TopNavbar = memo(function TopNavbar({
  onMenuClick,
  username,
  email,
  role,
  count,
  avatarUrl,
  onLogout,
}: {
  onMenuClick?: () => void;
  username: string;
  email: string;
  role: string;
  count: number;
  avatarUrl: string;
  onLogout: () => void;
}) {
  const router = useRouter();
  const avatarSrc = avatarUrl ? `${API_URL}${avatarUrl}` : "";
  const roleInfo = roleMeta(role);

  return (
    // h-16 matches the sidebar header so the two borders sit on one line.
    <header className="flex h-16 items-center gap-3 border-b border-slate-200/80 bg-white/80 px-4 backdrop-blur-md dark:border-slate-800 dark:bg-slate-900/80 lg:px-6">
      {/* Mobile menu */}
      {onMenuClick && (
        <button
          onClick={onMenuClick}
          aria-label="Open navigation menu"
          className="rounded-full p-2 text-slate-500 hover:bg-slate-100 hover:text-slate-700 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-200 lg:hidden"
        >
          <Menu className="h-5 w-5" />
        </button>
      )}

      <div className="ml-auto flex items-center">
        {/* Profile menu: identity, stats, account settings, sign out. */}
        <DropdownMenu
          onOpenChange={(open) => {
            if (open) window.dispatchEvent(new CustomEvent("profile-refresh"));
          }}
        >
          <DropdownMenuTrigger
            title={username}
            aria-label="Open profile menu"
            className="flex h-9 w-9 items-center justify-center rounded-full transition-shadow hover:ring-4 hover:ring-brand-500/15 focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-brand-500/25"
          >
            {/* The avatar fills the button. avatar-btn = pre-hydration initials overlay (globals.css). */}
            <Avatar src={avatarSrc} name={username} tone="solid" className="avatar-btn h-9 w-9 text-xs" />
          </DropdownMenuTrigger>

          <DropdownMenuContent className="w-72">
            {/* Identity */}
            <div className="flex items-center gap-3 px-2.5 pb-3 pt-2">
              <Avatar src={avatarSrc} name={username} size="lg" />
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-bold text-slate-900 dark:text-slate-100">{username}</p>
                {email && <p className="truncate text-xs text-slate-500 dark:text-slate-400">{email}</p>}
                {roleInfo.label && (
                  <Badge variant={roleInfo.variant} className="mt-1 uppercase tracking-wide">
                    {roleInfo.label}
                  </Badge>
                )}
              </div>
            </div>

            <DropdownMenuSeparator />
            {/* Stat row — same type, icon and padding as the menu items below. */}
            <div className="flex w-full items-center gap-2.5 px-3 py-2 text-sm font-medium text-slate-700">
              <FileSpreadsheet className="h-4 w-4 shrink-0 text-slate-400" />
              Total extractions
              <span className="ml-auto tabular-nums text-slate-700">{count.toLocaleString()}</span>
            </div>
            <DropdownMenuItem icon={Settings} onClick={() => router.push("/settings")}>
              Account settings
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem icon={LogOut} tone="danger" onClick={onLogout}>
              Sign out
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
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
  // Desktop rail state comes from SidebarProvider (root layout) — see the note
  // there for why it can't live in this per-page component.
  const { collapsed: sidebarCollapsed, toggle: toggleSidebar, animate: animateSidebar } = useContext(SidebarContext);
  const [mounted, setMounted] = useState(false);
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("");
  const [count, setCount] = useState(0);
  const [avatarUrl, setAvatarUrl] = useState("");
  const pathname = usePathname();

  const [isAdmin, setIsAdmin] = useState(false);

  useIsomorphicLayoutEffect(() => {
    const cachedName = localStorage.getItem("profile_username") ?? "";
    if (cachedName) setUsername(cachedName);

    const cachedAvatar = localStorage.getItem("profile_avatar_url") ?? "";
    if (cachedAvatar) setAvatarUrl(cachedAvatar);

    const ADMIN_ROLES = ["admin", "super_admin", "branch_admin"];
    const admin = ADMIN_ROLES.includes(getRole() ?? "");
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

  const handleLogout = useCallback(async () => {
    try {
      await fetch(`${API_URL}/auth/logout`, {
        method: "POST",
        headers: { ...authHeaders() },
      });
    } catch {
      // ignore
    }
    // Clear user-scoped batch session BEFORE clearToken() — token is needed to
    // compute the uid-scoped key (JWT uid claim).
    try {
      const token = localStorage.getItem("token");
      if (token) {
        const parts = token.split(".");
        if (parts.length === 3) {
          const b64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
          const payload = JSON.parse(atob(b64)) as Record<string, unknown>;
          const uid = String(payload.uid ?? payload.sub ?? "");
          if (uid) sessionStorage.removeItem(`spir_batch_session-${uid}`);
        }
      }
    } catch {
      // non-fatal — isolation is still guaranteed by user-scoped keys
    }
    clearSession(); // single-file extraction session (also user-scoped in extraction-session.ts)
    clearCompareSession(); // comparison result (user-scoped in compare-session.ts)
    resetCompareMemory();
    clearToken();
    localStorage.removeItem("profile_avatar_url");
    window.location.href = "/login";
  }, []);

  const handleMenuClick = useCallback(() => setMobileOpen(true), []);
  const handleCloseMobile = useCallback(() => setMobileOpen(false), []);
  return (
    <div className="flex h-screen overflow-hidden bg-slate-50 dark:bg-slate-950">
      {/* Desktop sidebar — width animates; the flex-1 main column follows it */}
      <aside
        id="app-sidebar"
        className={cn(
          "hidden shrink-0 overflow-hidden border-r border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900 lg:flex lg:flex-col",
          animateSidebar && "transition-[width] duration-200 ease-in-out",
          sidebarCollapsed ? "w-[68px]" : "w-60"
        )}
      >
        <SidebarContent
          pathname={pathname}
          isAdmin={mounted && isAdmin}
          collapsed={sidebarCollapsed}
          onToggle={toggleSidebar}
        />
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
          username={username}
          email={email}
          role={role}
          count={count}
          avatarUrl={avatarUrl}
          onLogout={handleLogout}
        />
        <main className="flex-1 overflow-y-auto">{children}</main>
      </div>
    </div>
  );
}
