"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Loader2, Eye, EyeOff, AlertCircle, User, Lock } from "lucide-react";
import { saveToken } from "@/lib/auth";

const API_URL = process.env.NEXT_PUBLIC_API_URL!;

export default function LoginPage() {
  const router = useRouter();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showPass, setShowPass] = useState(false);
  const [remember, setRemember] = useState(false);

  async function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    setLoading(true);

    const form = new FormData(e.currentTarget);
    const body = new URLSearchParams({
      username: form.get("username") as string,
      password: form.get("password") as string,
    });

    try {
      const res = await fetch(`${API_URL}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: body.toString(),
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setError(data.detail ?? "Invalid credentials. Please try again.");
        return;
      }

      const { access_token } = await res.json();
      saveToken(access_token);
      router.push("/extraction");
    } catch {
      setError("Could not connect to the server. Is the backend running?");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="relative flex min-h-screen flex-col items-center justify-center px-4">

      {/* ── Animated background: real <img> so CSS transform actually scales the image ── */}
      <div className="absolute inset-0 overflow-hidden">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src="/cdc_cover.jpg"
          alt=""
          aria-hidden="true"
          className="h-full w-full object-cover animate-bg-zoom"
        />
      </div>

      {/* ── Dark + purple overlay ── */}
      <div className="absolute inset-0 bg-black/50" />
      <div className="absolute inset-0 bg-gradient-to-br from-purple-950/40 to-transparent" />

      {/* ── Content ── */}
      <div className="relative z-10 w-full max-w-sm">
        {/* Logo + brand */}
        <div className="mb-8 flex flex-col items-center gap-4 text-center">
          <div className="relative flex h-20 w-20 items-center justify-center">
            {/* Soft glow ring */}
            <div className="absolute inset-0 rounded-3xl bg-white/20 blur-xl" />
            {/* Logo box — plain img to avoid Next.js Image config requirements */}
            <div className="relative h-20 w-20 overflow-hidden rounded-2xl bg-white shadow-2xl shadow-purple-900/50">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src="/cdc_logo.jpg"
                alt="CDC International"
                className="h-full w-full object-contain"
              />
            </div>
          </div>

          <div className="space-y-0.5">
            <h1 className="text-2xl font-extrabold tracking-[0.15em] text-white uppercase drop-shadow">
              SPIR TOOL
            </h1>
            <p className="text-xs font-medium tracking-widest text-white/60 uppercase">
              Spare Parts Intelligence Platform
            </p>
          </div>
        </div>

        {/* ── Login card ── */}
        <div className="rounded-2xl border border-white/20 bg-white/90 px-8 py-8 shadow-2xl backdrop-blur-sm">
          <div className="mb-6">
            <h2 className="text-xl font-bold text-slate-900">Welcome back</h2>
            <p className="mt-1 text-sm text-slate-500">
              Sign in to access your workspace
            </p>
          </div>

          <form onSubmit={handleSubmit} className="space-y-4">
            {/* Username */}
            <div className="space-y-1.5">
              <label htmlFor="username" className="text-sm font-medium text-slate-700">
                Username
              </label>
              <div className="relative">
                <User className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
                <input
                  id="username"
                  name="username"
                  type="text"
                  autoComplete="username"
                  required
                  placeholder="Enter your username"
                  className="h-10 w-full rounded-lg border border-slate-200 bg-white pl-9 pr-3 text-sm text-slate-900 placeholder:text-slate-400 focus:border-violet-500 focus:outline-none focus:ring-2 focus:ring-violet-500/20 transition-colors"
                />
              </div>
            </div>

            {/* Password */}
            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <label htmlFor="password" className="text-sm font-medium text-slate-700">
                  Password
                </label>
                <button
                  type="button"
                  className="text-xs text-violet-600 hover:text-violet-700 hover:underline"
                >
                  Forgot Password?
                </button>
              </div>
              <div className="relative">
                <Lock className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
                <input
                  id="password"
                  name="password"
                  type={showPass ? "text" : "password"}
                  autoComplete="current-password"
                  required
                  placeholder="Enter your password"
                  className="h-10 w-full rounded-lg border border-slate-200 bg-white pl-9 pr-10 text-sm text-slate-900 placeholder:text-slate-400 focus:border-violet-500 focus:outline-none focus:ring-2 focus:ring-violet-500/20 transition-colors"
                />
                <button
                  type="button"
                  onClick={() => setShowPass((p) => !p)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600"
                  tabIndex={-1}
                >
                  {showPass ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
            </div>

            {/* Remember me */}
            <div className="flex items-center gap-2">
              <input
                id="remember"
                type="checkbox"
                checked={remember}
                onChange={(e) => setRemember(e.target.checked)}
                className="h-4 w-4 rounded border-slate-300 text-violet-600 focus:ring-violet-500"
              />
              <label htmlFor="remember" className="text-sm text-slate-600 cursor-pointer">
                Remember this device
              </label>
            </div>

            {/* Error */}
            {error && (
              <div className="flex items-start gap-2.5 rounded-lg bg-red-50 p-3 text-sm text-red-600">
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>{error}</span>
              </div>
            )}

            {/* Submit */}
            <button
              type="submit"
              disabled={loading}
              className="mt-2 flex h-11 w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-violet-700 to-purple-700 text-sm font-semibold text-white shadow-md shadow-purple-300/50 transition-all hover:from-violet-800 hover:to-purple-800 disabled:opacity-60"
            >
              {loading ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Signing in…
                </>
              ) : (
                <>
                  Sign In
                  <span className="ml-1">→</span>
                </>
              )}
            </button>
          </form>
        </div>

        {/* Footer */}
        <div className="mt-6 flex items-center justify-center gap-4 text-xs text-white/40">
          <button className="hover:text-white/70 hover:underline transition-colors">Privacy Policy</button>
          <span>·</span>
          <button className="hover:text-white/70 hover:underline transition-colors">Terms of Service</button>
          <span>·</span>
          <button className="hover:text-white/70 hover:underline transition-colors">Support</button>
        </div>
      </div>
    </div>
  );
}
