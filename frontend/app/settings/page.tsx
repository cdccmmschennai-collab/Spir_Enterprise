"use client";

import { useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Clock,
  FileSpreadsheet,
  ImageOff,
  ImagePlus,
  KeyRound,
  Lock,
  Pencil,
  RefreshCw,
} from "lucide-react";
import { SidebarLayout } from "@/components/sidebar";
import { authHeaders } from "@/lib/auth";
import { roleMeta } from "@/lib/roles";
import { PageHeader } from "@/components/ui/page-header";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Avatar } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { PasswordInput } from "@/components/ui/password-input";
import { FieldHint, Label } from "@/components/ui/label";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

interface Profile {
  username: string;
  email: string | null;
  role: string;
  branch_id: string | null;
  branch_name: string | null;
  created_at: string | null;
  last_login_at: string | null;
  total_files_extracted: number;
  avatar_url: string | null;
}

function formatDate(iso: string | null) {
  if (!iso) return "-";
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

function Stat({ icon: Icon, label, value }: { icon: React.ElementType; label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center gap-3 rounded-2xl bg-slate-50 px-4 py-3 dark:bg-slate-800/60">
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-white text-brand-700 shadow-sm dark:bg-slate-900 dark:text-brand-300">
        <Icon className="h-4 w-4" />
      </span>
      <div className="min-w-0">
        <p className="text-[11px] font-medium text-slate-500 dark:text-slate-400">{label}</p>
        <p className="truncate text-sm font-bold text-slate-900 dark:text-slate-100">{value}</p>
      </div>
    </div>
  );
}

export default function SettingsPage() {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [avatarSrc, setAvatarSrc] = useState("");
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Password change form state — the form stays hidden until the user asks for it.
  const [pwOpen, setPwOpen] = useState(false);
  const [pwCurrent, setPwCurrent] = useState("");
  const [pwNew, setPwNew] = useState("");
  const [pwConfirm, setPwConfirm] = useState("");
  const [pwLoading, setPwLoading] = useState(false);
  const [pwError, setPwError] = useState<string | null>(null);
  const [pwSuccess, setPwSuccess] = useState(false);

  useEffect(() => {
    fetch(`${API_URL}/api/me`, { headers: { ...authHeaders() } })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data) {
          setProfile(data);
          if (data.avatar_url) setAvatarSrc(`${API_URL}${data.avatar_url}`);
        }
      })
      .catch(() => {});
  }, []);

  function openPasswordForm() {
    setPwSuccess(false);
    setPwOpen(true);
  }

  function closePasswordForm() {
    setPwOpen(false);
    setPwError(null);
    setPwCurrent(""); setPwNew(""); setPwConfirm("");
  }

  async function handlePasswordChange(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setPwError(null);
    setPwSuccess(false);
    if (pwNew !== pwConfirm) {
      setPwError("New passwords do not match.");
      return;
    }
    if (pwNew.length < 8) {
      setPwError("Password must be at least 8 characters.");
      return;
    }
    if (!/[A-Z]/.test(pwNew)) { setPwError("Password must contain at least one uppercase letter."); return; }
    if (!/[a-z]/.test(pwNew)) { setPwError("Password must contain at least one lowercase letter."); return; }
    if (!/\d/.test(pwNew))    { setPwError("Password must contain at least one digit."); return; }

    setPwLoading(true);
    try {
      const res = await fetch(`${API_URL}/auth/change-password`, {
        method: "PUT",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ current_password: pwCurrent, new_password: pwNew }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setPwError(data.detail ?? `Failed (${res.status})`);
        return;
      }
      closePasswordForm();
      setPwSuccess(true);
      setTimeout(() => setPwSuccess(false), 5000);
    } catch {
      setPwError("Network error. Please try again.");
    } finally {
      setPwLoading(false);
    }
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

  const role = roleMeta(profile?.role);
  // Super admins aren't tied to one branch; everyone else should have one.
  const branchLabel = !profile
    ? ""
    : profile.branch_name ?? (profile.role === "super_admin" ? "All branches" : "Not assigned");

  return (
    <SidebarLayout>
      <div className="mx-auto max-w-3xl space-y-6 p-4 sm:p-6 lg:p-8">
        <PageHeader back title="Account settings" description="Manage your profile and password." />

        {/* 1. Profile */}
        <Card>
          <CardContent className="space-y-6">
            <div className="flex items-center gap-4">
              <div className="relative shrink-0">
                <Avatar src={avatarSrc} name={profile?.username} size="xl" />
                <DropdownMenu>
                  <DropdownMenuTrigger
                    disabled={uploading}
                    title="Edit photo"
                    aria-label="Edit profile photo"
                    className="absolute -bottom-1 -right-1 flex h-7 w-7 items-center justify-center rounded-full border-2 border-white bg-brand-700 text-white shadow-sm transition-colors hover:bg-brand-800 disabled:opacity-60 dark:border-slate-900"
                  >
                    <Pencil className="h-3 w-3" />
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="start" className="min-w-[13rem]">
                    <DropdownMenuItem icon={avatarSrc ? RefreshCw : ImagePlus} onClick={() => fileInputRef.current?.click()}>
                      {avatarSrc ? "Change photo" : "Upload photo"}
                    </DropdownMenuItem>
                    {avatarSrc && (
                      <DropdownMenuItem icon={ImageOff} tone="danger" onClick={handleAvatarRemove}>
                        Remove photo
                      </DropdownMenuItem>
                    )}
                  </DropdownMenuContent>
                </DropdownMenu>
              </div>
              <div className="min-w-0">
                <p className="truncate text-lg font-bold text-slate-900 dark:text-slate-100">{profile?.username ?? ""}</p>
                {role.label && (
                  <Badge variant={role.variant} className="mt-1 uppercase tracking-wide">
                    {role.label}
                  </Badge>
                )}
                {uploading && <p className="mt-1 text-xs text-slate-400">Updating photo…</p>}
              </div>
            </div>

            <input
              ref={fileInputRef}
              type="file"
              accept="image/jpeg,image/png,image/webp,image/gif"
              className="hidden"
              onChange={handleAvatarUpload}
            />

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="st-email">Email</Label>
                <Input id="st-email" value={profile?.email ?? "Not set"} readOnly />
              </div>
              <div>
                <Label htmlFor="st-branch">Branch</Label>
                <Input id="st-branch" value={branchLabel} readOnly />
              </div>
            </div>

            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Stat icon={FileSpreadsheet} label="Extractions" value={(profile?.total_files_extracted ?? 0).toLocaleString()} />
              <Stat icon={Clock} label="Last login" value={formatDate(profile?.last_login_at ?? null)} />
            </div>
          </CardContent>
        </Card>

        {/* 2. Security — Change Password (form opens on demand) */}
        <Card>
          <CardHeader
            icon={Lock}
            action={
              !pwOpen && (
                <Button variant="outline" size="sm" onClick={openPasswordForm}>
                  <KeyRound />
                  Change password
                </Button>
              )
            }
          >
            <CardTitle>Password</CardTitle>
            <CardDescription>Other active sessions are signed out after a change.</CardDescription>
          </CardHeader>

          {pwSuccess && !pwOpen && (
            <div className="px-6 py-4">
              <div role="status" className="flex items-start gap-2.5 rounded-xl bg-emerald-50 p-3 text-sm text-emerald-700">
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
                Password changed. Other active sessions have been signed out.
              </div>
            </div>
          )}

          {pwOpen && (
            <CardContent>
              <form onSubmit={handlePasswordChange} className="space-y-4">
                <div>
                  <Label htmlFor="pw-current">Current password</Label>
                  <PasswordInput
                    id="pw-current"
                    value={pwCurrent}
                    onChange={(e) => setPwCurrent(e.target.value)}
                    required
                    autoFocus
                    autoComplete="current-password"
                    placeholder="Enter current password"
                  />
                </div>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <div>
                    <Label htmlFor="pw-new">New password</Label>
                    <PasswordInput
                      id="pw-new"
                      value={pwNew}
                      onChange={(e) => setPwNew(e.target.value)}
                      required
                      minLength={8}
                      autoComplete="new-password"
                      placeholder="Min. 8 characters"
                    />
                    <FieldHint>Uppercase, lowercase and a digit.</FieldHint>
                  </div>
                  <div>
                    <Label htmlFor="pw-confirm">Confirm new password</Label>
                    <PasswordInput
                      id="pw-confirm"
                      value={pwConfirm}
                      onChange={(e) => setPwConfirm(e.target.value)}
                      required
                      minLength={8}
                      autoComplete="new-password"
                      placeholder="Repeat new password"
                    />
                  </div>
                </div>

                {pwError && (
                  <div role="alert" className="flex items-start gap-2.5 rounded-xl bg-red-50 p-3 text-sm text-red-700">
                    <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                    {pwError}
                  </div>
                )}

                <div className="flex justify-end gap-2">
                  <Button type="button" variant="ghost" onClick={closePasswordForm} disabled={pwLoading}>
                    Cancel
                  </Button>
                  <Button type="submit" disabled={pwLoading || !pwCurrent || !pwNew || !pwConfirm}>
                    {pwLoading ? "Updating…" : "Update password"}
                  </Button>
                </div>
              </form>
            </CardContent>
          )}
        </Card>      </div>
    </SidebarLayout>
  );
}
