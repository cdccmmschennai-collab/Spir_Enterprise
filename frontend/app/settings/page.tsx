"use client";

import { useEffect, useRef, useState } from "react";
import { User, Moon, Sun, FileSpreadsheet, Clock, Pencil, Settings2, ImagePlus, ImageOff, RefreshCw } from "lucide-react";
import { SidebarLayout } from "@/components/sidebar";
import { authHeaders } from "@/lib/auth";
import { cn } from "@/lib/utils";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

interface Profile {
  username: string;
  email: string | null;
  role: string;
  created_at: string | null;
  last_login_at: string | null;
  total_files_extracted: number;
  avatar_url: string | null;
}

function SectionCard({ title, icon: Icon, children }: { title: string; icon: React.ElementType; children: React.ReactNode }) {
  return (
    <div className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 shadow-sm overflow-hidden">
      <div className="flex items-center gap-2.5 border-b border-slate-100 dark:border-slate-700 px-5 py-4">
        <Icon className="h-4 w-4 text-violet-600" />
        <h2 className="text-sm font-bold text-slate-800 dark:text-slate-200">{title}</h2>
      </div>
      <div className="p-5">{children}</div>
    </div>
  );
}

function ReadOnlyField({ label, value }: { label: string; value: string }) {
  return (
    <div className="space-y-1">
      <label className="text-xs font-semibold uppercase tracking-wide text-slate-400 dark:text-slate-500">{label}</label>
      <div className="flex h-9 items-center rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900 px-3 text-sm text-slate-700 dark:text-slate-300">
        {value || "—"}
      </div>
    </div>
  );
}

export default function SettingsPage() {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [darkMode, setDarkMode] = useState(false);
  const [avatarSrc, setAvatarSrc] = useState("");
  const [uploading, setUploading] = useState(false);
  const [showAvatarMenu, setShowAvatarMenu] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const avatarMenuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (avatarMenuRef.current && !avatarMenuRef.current.contains(e.target as Node)) {
        setShowAvatarMenu(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  useEffect(() => {
    setDarkMode(localStorage.getItem("theme") === "dark");

    fetch(`${API_URL}/api/me`, { headers: { ...authHeaders() } })
      .then((r) => r.ok ? r.json() : null)
      .then((data) => {
        if (data) {
          setProfile(data);
          if (data.avatar_url) setAvatarSrc(`${API_URL}${data.avatar_url}`);
        }
      })
      .catch(() => {});
  }, []);

  function toggleTheme() {
    const next = !darkMode;
    setDarkMode(next);
    localStorage.setItem("theme", next ? "dark" : "light");
    document.documentElement.classList.toggle("dark", next);
  }

  function formatDate(iso: string | null) {
    if (!iso) return "—";
    return new Date(iso).toLocaleString("en-IN", {
      timeZone: "Asia/Kolkata",
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: true,
    });
  }

  async function handleAvatarUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      const localUrl = URL.createObjectURL(file);
      setAvatarSrc(localUrl);

      const form = new FormData();
      form.append("file", file);
      const res = await fetch(`${API_URL}/api/avatar`, {
        method: "POST",
        headers: { ...authHeaders() },
        body: form,
      });
      if (res.ok) {
        const data = await res.json();
        setAvatarSrc(`${API_URL}${data.avatar_url}`);
        localStorage.setItem("profile_avatar_url", data.avatar_url);
        window.dispatchEvent(new CustomEvent("profile-refresh"));
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
        setAvatarSrc("");
        localStorage.removeItem("profile_avatar_url");
        window.dispatchEvent(new CustomEvent("profile-refresh"));
      }
    } catch {
      // ignore
    } finally {
      setUploading(false);
    }
  }

  const initials = profile?.username?.slice(0, 2).toUpperCase() ?? "—";

  return (
    <SidebarLayout>
      <div className="mx-auto max-w-2xl space-y-6 p-4 sm:p-6 lg:p-8">
        <div>
          <h1 className="text-xl font-bold text-slate-900 dark:text-slate-100">Settings</h1>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
            Manage your account and workspace preferences
          </p>
        </div>

        {/* 1. Profile */}
        <SectionCard title="Profile" icon={User}>
          <div className="space-y-4">
            {/* Avatar row */}
            <div className="flex items-center gap-4">
              <div className="relative shrink-0" ref={avatarMenuRef}>
                <div className="flex h-16 w-16 items-center justify-center rounded-full overflow-hidden bg-violet-100 dark:bg-violet-900 shadow-sm ring-2 ring-violet-200 dark:ring-violet-800">
                  {avatarSrc ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img src={avatarSrc} alt="Avatar" className="h-full w-full object-cover" />
                  ) : (
                    <span className="text-xl font-bold text-violet-700 dark:text-violet-300">{initials}</span>
                  )}
                </div>
                <button
                  onClick={() => setShowAvatarMenu((p) => !p)}
                  disabled={uploading}
                  title="Edit photo"
                  className="absolute bottom-0 right-0 flex h-6 w-6 items-center justify-center rounded-full bg-violet-600 text-white hover:bg-violet-700 transition-colors shadow-sm disabled:opacity-60"
                >
                  <Pencil className="h-3 w-3" />
                </button>
                {showAvatarMenu && (
                  <div className="absolute top-full left-0 mt-1.5 z-50 min-w-[168px] rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 shadow-lg overflow-hidden">
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
              <div>
                <p className="text-base font-bold text-slate-800 dark:text-slate-100">{profile?.username ?? "—"}</p>
                <span className={cn(
                  "inline-flex items-center rounded-full px-2.5 py-0.5 text-[10px] font-bold uppercase tracking-wide",
                  profile?.role === "admin"
                    ? "bg-violet-100 text-violet-700 dark:bg-violet-950 dark:text-violet-300"
                    : "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300"
                )}>
                  {profile?.role ?? "—"}
                </span>
                {uploading && (
                  <p className="mt-1 text-xs text-slate-400">Updating…</p>
                )}
              </div>
            </div>

            <input
              ref={fileInputRef}
              type="file"
              accept="image/jpeg,image/png,image/webp,image/gif"
              className="hidden"
              onChange={handleAvatarUpload}
            />

            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <ReadOnlyField label="Username" value={profile?.username ?? ""} />
              <ReadOnlyField label="Email" value={profile?.email ?? "Not set"} />
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="flex items-center gap-2 rounded-lg border border-slate-100 dark:border-slate-700 bg-slate-50 dark:bg-slate-900 px-3 py-2.5">
                <FileSpreadsheet className="h-3.5 w-3.5 text-violet-500" />
                <div>
                  <p className="text-[10px] text-slate-400 dark:text-slate-500">Extractions</p>
                  <p className="text-sm font-bold text-slate-800 dark:text-slate-100">{profile?.total_files_extracted ?? 0}</p>
                </div>
              </div>
              <div className="flex items-center gap-2 rounded-lg border border-slate-100 dark:border-slate-700 bg-slate-50 dark:bg-slate-900 px-3 py-2.5">
                <Clock className="h-3.5 w-3.5 text-emerald-500" />
                <div>
                  <p className="text-[10px] text-slate-400 dark:text-slate-500">Last login</p>
                  <p className="text-[11px] font-medium text-slate-600 dark:text-slate-300 leading-tight">{formatDate(profile?.last_login_at ?? null)}</p>
                </div>
              </div>
            </div>
          </div>
        </SectionCard>

        {/* 2. Preferences */}
        <SectionCard title="Preferences" icon={Settings2}>
          <div className="space-y-3">
            <div className="flex items-center justify-between rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900 px-4 py-3">
              <div className="flex items-center gap-2.5">
                {darkMode ? <Moon className="h-4 w-4 text-violet-500" /> : <Sun className="h-4 w-4 text-amber-500" />}
                <div>
                  <p className="text-sm font-medium text-slate-700 dark:text-slate-200">Appearance</p>
                  <p className="text-xs text-slate-400">{darkMode ? "Dark mode" : "Light mode"}</p>
                </div>
              </div>
              <button
                onClick={toggleTheme}
                className={cn(
                  "relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 focus:outline-none",
                  darkMode ? "bg-violet-600" : "bg-slate-300"
                )}
                role="switch"
                aria-checked={darkMode}
              >
                <span className={cn(
                  "pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow-lg ring-0 transition duration-200 ease-in-out",
                  darkMode ? "translate-x-5" : "translate-x-0"
                )} />
              </button>
            </div>
          </div>
        </SectionCard>
      </div>
    </SidebarLayout>
  );
}
