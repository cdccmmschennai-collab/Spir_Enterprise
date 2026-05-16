"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Loader2, Eye, EyeOff, AlertCircle, User, Lock, Mail, ArrowLeft, CheckCircle2 } from "lucide-react";
import { saveToken, saveRole } from "@/lib/auth";
import { clearSession } from "@/lib/extraction-session";

const API_URL = process.env.NEXT_PUBLIC_API_URL!;

// ─── Forgot Password Form ─────────────────────────────────────────────────────

function ForgotPasswordForm({ onBack }: { onBack: () => void }) {
  const [loading, setLoading] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    const form = new FormData(e.currentTarget);
    try {
      const res = await fetch(`${API_URL}/auth/reset-request`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: form.get("username"),
          email: form.get("email") || null,
          reason: form.get("reason") || null,
        }),
      });
      // Always show success regardless of status to avoid leaking account info
      if (res.ok || res.status === 201) {
        setSubmitted(true);
      } else {
        setError("Could not submit request. Please try again or contact your administrator.");
      }
    } catch {
      setError("Could not connect to the server.");
    } finally {
      setLoading(false);
    }
  }

  if (submitted) {
    return (
      <div className="text-center space-y-4">
        <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-emerald-100">
          <CheckCircle2 className="h-6 w-6 text-emerald-600" />
        </div>
        <div>
          <h3 className="text-base font-bold text-slate-900">Request Submitted</h3>
          <p className="mt-1 text-sm text-slate-500">
            Your request has been sent to the administrator for approval. You will be notified once your access is restored.
          </p>
        </div>
        <button
          onClick={onBack}
          className="text-sm font-medium text-violet-600 hover:text-violet-700 hover:underline"
        >
          ← Back to Sign In
        </button>
      </div>
    );
  }

  return (
    <>
      <div className="mb-5">
        <button
          type="button"
          onClick={onBack}
          className="mb-3 flex items-center gap-1.5 text-xs text-slate-400 hover:text-slate-600 transition-colors"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          Back to Sign In
        </button>
        <h2 className="text-xl font-bold text-slate-900">Reset Password</h2>
        <p className="mt-1 text-sm text-slate-500">
          Submit a request to your administrator. They will reset your access.
        </p>
      </div>

      <form onSubmit={handleSubmit} className="space-y-4">
        <div className="space-y-1.5">
          <label htmlFor="fp-username" className="text-sm font-medium text-slate-700">
            Username <span className="text-red-500">*</span>
          </label>
          <div className="relative">
            <User className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
            <input
              id="fp-username"
              name="username"
              type="text"
              required
              autoComplete="username"
              placeholder="Your username"
              className="h-10 w-full rounded-lg border border-slate-200 bg-white pl-9 pr-3 text-sm text-slate-900 placeholder:text-slate-400 focus:border-violet-500 focus:outline-none focus:ring-2 focus:ring-violet-500/20 transition-colors"
            />
          </div>
        </div>

        <div className="space-y-1.5">
          <label htmlFor="fp-email" className="text-sm font-medium text-slate-700">
            Email <span className="text-slate-400 font-normal">(optional — helps admin identify you)</span>
          </label>
          <div className="relative">
            <Mail className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
            <input
              id="fp-email"
              name="email"
              type="email"
              autoComplete="email"
              placeholder="your@email.com"
              className="h-10 w-full rounded-lg border border-slate-200 bg-white pl-9 pr-3 text-sm text-slate-900 placeholder:text-slate-400 focus:border-violet-500 focus:outline-none focus:ring-2 focus:ring-violet-500/20 transition-colors"
            />
          </div>
        </div>

        <div className="space-y-1.5">
          <label htmlFor="fp-reason" className="text-sm font-medium text-slate-700">
            Reason <span className="text-slate-400 font-normal">(optional)</span>
          </label>
          <textarea
            id="fp-reason"
            name="reason"
            rows={2}
            maxLength={500}
            placeholder="e.g. Forgot password, account locked..."
            className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 focus:border-violet-500 focus:outline-none focus:ring-2 focus:ring-violet-500/20 transition-colors resize-none"
          />
        </div>

        {error && (
          <div className="flex items-start gap-2.5 rounded-lg bg-red-50 p-3 text-sm text-red-600">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        <button
          type="submit"
          disabled={loading}
          className="mt-1 flex h-11 w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-violet-700 to-purple-700 text-sm font-semibold text-white shadow-md shadow-purple-300/50 transition-all hover:from-violet-800 hover:to-purple-800 disabled:opacity-60"
        >
          {loading ? (
            <>
              <Loader2 className="h-4 w-4 animate-spin" />
              Submitting…
            </>
          ) : (
            "Submit Request to Admin"
          )}
        </button>
      </form>
    </>
  );
}

// ─── Login Page ───────────────────────────────────────────────────────────────

export default function LoginPage() {
  const router = useRouter();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showPass, setShowPass] = useState(false);
  const [remember, setRemember] = useState(false);
  const [forgotMode, setForgotMode] = useState(false);

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
      clearSession();
      saveToken(access_token);

      try {
        const meRes = await fetch(`${API_URL}/api/me`, {
          headers: { Authorization: `Bearer ${access_token}` },
        });
        if (meRes.ok) {
          const meData = await meRes.json();
          saveRole(meData.role ?? "user");
        } else {
          saveRole("user");
        }
      } catch {
        saveRole("user");
      }

      router.push("/extraction");
    } catch {
      setError("Could not connect to the server. Is the backend running?");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="relative flex min-h-screen flex-col items-center justify-center px-4">

      {/* Animated background */}
      <div className="absolute inset-0 overflow-hidden">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src="/cdc_cover.jpg"
          alt=""
          aria-hidden="true"
          className="h-full w-full object-cover animate-bg-zoom"
        />
      </div>

      {/* Overlays */}
      <div className="absolute inset-0 bg-black/50" />
      <div className="absolute inset-0 bg-gradient-to-br from-purple-950/40 to-transparent" />

      {/* Content */}
      <div className="relative z-10 w-full max-w-sm">
        {/* Logo + brand */}
        <div className="mb-8 flex flex-col items-center gap-4 text-center">
          <div className="relative flex h-20 w-20 items-center justify-center">
            <div className="absolute inset-0 rounded-3xl bg-white/20 blur-xl" />
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
              SPARE PARTS INTERCHANGEABILITY RECORD
            </p>
          </div>
        </div>

        {/* Card */}
        <div className="rounded-2xl border border-white/20 bg-white/90 px-8 py-8 shadow-2xl backdrop-blur-sm">
          {forgotMode ? (
            <ForgotPasswordForm onBack={() => setForgotMode(false)} />
          ) : (
            <>
              <div className="mb-6">
                <h2 className="text-xl font-bold text-slate-900">Welcome to CDC</h2>
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
                      onClick={() => setForgotMode(true)}
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
            </>
          )}
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
