"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Plus_Jakarta_Sans } from "next/font/google";
import {
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  BookOpen,
  CheckCircle2,
  Coins,
  Download,
  Eye,
  EyeOff,
  Factory,
  FileSpreadsheet,
  GitCompare,
  Hash,
  Layers,
  Loader2,
  Lock,
  Mail,
  ShieldCheck,
  Tags,
  UploadCloud,
  UserRound,
} from "lucide-react";
import { saveToken, saveRole, saveBranchId } from "@/lib/auth";
import { clearSession } from "@/lib/extraction-session";
import { clearCompareSession, resetCompareMemory } from "@/lib/compare-session";

const API_URL = process.env.NEXT_PUBLIC_API_URL!;

const jakarta = Plus_Jakarta_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
});

// Shared field styling: soft pill-ish inputs with a navy focus glow (CDC palette).
const inputCls =
  "h-11 w-full rounded-xl border border-slate-200/80 bg-slate-50 pl-10 pr-4 text-sm text-slate-900 " +
  "placeholder:text-slate-400 transition-all focus:border-cdc-navy/40 focus:bg-white focus:outline-none " +
  "focus:ring-4 focus:ring-cdc-navy/10";
const labelCls = "mb-1.5 block text-xs font-semibold text-slate-800";
const iconCls = "pointer-events-none absolute left-3.5 top-1/2 h-[18px] w-[18px] -translate-y-1/2 text-slate-400";

// ─── Turning brand mark ───────────────────────────────────────────────────────
// The bare logo turns around its vertical axis (left → right, like a coin),
// modelled on emergent.sh's hero "e". Two faces: the back face is pre-flipped so
// the lettering never reads mirrored mid-turn. Keyframes live in globals.css.

function BrandMark() {
  const face = "cdc-coin-face h-[90px] w-[104px] object-contain";
  return (
    <div className="cdc-coin mb-6">
      <div className="cdc-coin-inner relative">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src="/cdc-logo.png" alt="CDC — Construction Development Company" className={face} />
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src="/cdc-logo.png" alt="" aria-hidden="true" className={`${face} cdc-coin-back absolute inset-0`} />
      </div>
    </div>
  );
}

// ─── Decorative peripheral cards (lg+ only) ───────────────────────────────────
// Two static elliptical lines, both tall and running off the top and bottom of
// the page (the user's sketch): 6 compact cards on the inner line, 6 on the
// outer. Cards stay level (no tilt). Both rings move at the same angular speed
// in the same direction, with outer cards half a slot between inner ones, so
// their relative layout is rigid and cards never collide.
//
// The radii were chosen by an offline sweep (every 2° of a lap, at 1024×768,
// 1280×900, 1440×900, 1920×1080) proving, for the fixed card footprints
// below, that no card ever overlaps another card or the content column — the
// worst case being the taller forgot-password form (440 × ~718px; checked at
// 440 × 740 + 12px gap). They're fixed px/width-based so the lines never move
// when the content changes height. One rAF loop positions everything.
// Not rendered below lg or for prefers-reduced-motion.

const RING_LAP_S = 64;
const RINGS = {
  inner: { rx: (w: number) => Math.max(0.36 * w, 360), ry: 950, phase: 0 },
  outer: { rx: (w: number) => 0.44 * w, ry: 1050, phase: 0.5 }, // half a slot offset
} as const;

function PeripheralCards() {
  const rootRef = useRef<HTMLDivElement>(null);
  const outerRefs = useRef<(HTMLDivElement | null)[]>([]);
  const innerRefs = useRef<(HTMLDivElement | null)[]>([]);
  const [enabled, setEnabled] = useState(false);

  // Decide on the client only: desktop width and motion allowed.
  useEffect(() => {
    const mq = window.matchMedia("(min-width: 1024px) and (prefers-reduced-motion: no-preference)");
    const update = () => setEnabled(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    if (!enabled) return;
    let frame = 0;
    const place = (
      els: (HTMLDivElement | null)[],
      ring: (typeof RINGS)[keyof typeof RINGS],
      cx: number,
      cy: number,
      w: number,
      turn: number,
    ) => {
      const rx = ring.rx(w);
      els.forEach((el, i) => {
        if (!el) return;
        const a = 2 * Math.PI * (turn + (i + ring.phase) / els.length);
        el.style.transform = `translate(${cx + rx * Math.cos(a)}px, ${cy + ring.ry * Math.sin(a)}px) translate(-50%, -50%)`;
        el.style.visibility = "visible";
      });
    };
    const tick = (now: number) => {
      const root = rootRef.current;
      if (root) {
        // The content column is centred in this container, so its centre is the rings' centre.
        const { width, height } = root.getBoundingClientRect();
        const turn = now / 1000 / RING_LAP_S;
        place(innerRefs.current, RINGS.inner, width / 2, height / 2, width, turn);
        place(outerRefs.current, RINGS.outer, width / 2, height / 2, width, turn);
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [enabled]);

  if (!enabled) return null;

  // Cards start hidden at the origin; the first frame positions and reveals them.
  const slot = "absolute left-0 top-0 invisible will-change-transform";
  return (
    <div ref={rootRef} aria-hidden="true" className="pointer-events-none absolute inset-0 select-none">
      {OUTER_CARDS.map((card, i) => (
        <div key={`o${i}`} ref={(el) => { outerRefs.current[i] = el; }} className={slot}>
          {card}
        </div>
      ))}
      {INNER_CARDS.map((card, i) => (
        <div key={`i${i}`} ref={(el) => { innerRefs.current[i] = el; }} className={slot}>
          {card}
        </div>
      ))}
    </div>
  );
}
// Fixed card footprints: the ring geometry below is proven collision-free for
// exactly these sizes, so they must stay in sync with the RINGS sweep (outer 240×124, inner 192×84).
const cardBase =
  "overflow-hidden border border-slate-200/70 bg-white/90 backdrop-blur-md shadow-[0_30px_60px_-15px_rgba(0,58,137,0.14)]";
const outerCard = `${cardBase} h-[124px] w-60 rounded-3xl p-4 opacity-85`;
const innerCard = `${cardBase} h-[84px] w-48 rounded-2xl p-3 opacity-90`;

const OUTER_CARDS: React.ReactNode[] = [
  // Extraction rows
  <div key="extract" className={outerCard}>
    <div className="flex items-center justify-between pb-3">
      <div className="flex items-center gap-2">
        <span className="h-2.5 w-2.5 rounded-full bg-cdc-gold" />
        <span className="text-[11px] font-bold uppercase tracking-wider text-slate-500">SPIR Extraction</span>
      </div>
      <FileSpreadsheet className="h-4 w-4 text-cdc-navy" />
    </div>
    <div className="rounded-xl bg-slate-50 p-2.5">
      <p className="text-xs font-semibold text-slate-800">Impeller, Pump P-2301A</p>
      <p className="text-[11px] text-slate-500">Item 12 · Qty 2 · Initial Spares</p>
    </div>
  </div>,

  // Tag expansion
  <div key="tags" className={outerCard}>
    <div className="flex items-center gap-3 pb-3">
      <div className="flex h-8 w-8 items-center justify-center rounded-full bg-cdc-gold/20 text-cdc-navy">
        <Tags className="h-4 w-4" />
      </div>
      <div>
        <p className="text-[13px] font-semibold text-slate-800">Tag expansion</p>
        <p className="text-[11px] text-slate-500">From annexure reference</p>
      </div>
    </div>
    <div className="rounded-xl bg-slate-50 p-2.5 font-mono text-[10px] text-slate-600">
      <span className="text-slate-800">23V01-A/B</span>
      <span className="mx-1.5 text-cdc-navy">→</span>
      <span className="rounded-md bg-white px-1.5 py-0.5 text-cdc-navy shadow-sm">23V01-A</span>{" "}
      <span className="rounded-md bg-white px-1.5 py-0.5 text-cdc-navy shadow-sm">23V01-B</span>
    </div>
  </div>,

  // Continuation mapping
  <div key="cont" className={outerCard}>
    <div className="mb-2 flex items-center justify-between">
      <span className="text-[13px] font-semibold text-slate-800">Continuation map</span>
      <Layers className="h-4 w-4 text-cdc-navy" />
    </div>
    <div className="space-y-1 text-[11px]">
      <div className="flex items-center justify-between rounded-lg bg-slate-50 p-1.5">
        <span className="text-slate-700">Sheet 2 · Item 07</span>
        <span className="font-medium text-cdc-navy">→ Main item 07</span>
      </div>
      <div className="flex items-center justify-between rounded-lg bg-slate-50/60 p-1.5">
        <span className="text-slate-700">Annexure 1</span>
        <span className="font-medium text-cdc-navy">→ 48 tags</span>
      </div>
    </div>
  </div>,

  // Model / serial
  <div key="model" className={outerCard}>
    <div className="mb-2 flex items-center gap-2">
      <div className="flex h-7 w-7 items-center justify-center rounded-full bg-cdc-navy/10 text-cdc-navy">
        <Hash className="h-3.5 w-3.5" />
      </div>
      <span className="text-[13px] font-semibold text-slate-800">Model &amp; serial</span>
    </div>
    <div className="space-y-1 text-[11px] text-slate-600">
      <div className="flex justify-between rounded-lg bg-slate-50 px-2 py-1">
        <span>Model</span>
        <span className="font-mono text-cdc-navy">3196-MTX</span>
      </div>
      <div className="flex justify-between rounded-lg bg-slate-50/60 px-2 py-1">
        <span>Serial</span>
        <span className="font-mono text-cdc-navy">SN-204871</span>
      </div>
    </div>
  </div>,

  // Annexure
  <div key="annex" className={outerCard}>
    <div className="flex items-center justify-between">
      <span className="text-[13px] font-semibold text-slate-800">Annexure 2</span>
      <BookOpen className="h-4 w-4 text-cdc-navy" />
    </div>
    <p className="mt-1 text-[11px] text-slate-500">Referenced from item 21</p>
    <div className="mt-3 h-1.5 w-full rounded-full bg-slate-100">
      <div className="h-1.5 w-4/5 rounded-full bg-cdc-gold" />
    </div>
    <p className="mt-1.5 text-right text-[11px] font-medium text-cdc-navy">112 tags linked</p>
  </div>,

  // Data checks
  <div key="checks" className={outerCard}>
    <div className="mb-2 flex items-center justify-between">
      <span className="text-[13px] font-semibold text-slate-800">Data checks</span>
      <ShieldCheck className="h-4 w-4 text-emerald-600" />
    </div>
    <ul className="space-y-1 text-[11px] text-slate-600">
      <li className="flex items-center gap-1.5">
        <CheckCircle2 className="h-3.5 w-3.5 text-emerald-600" /> Tag column clean
      </li>
      <li className="flex items-center gap-1.5">
        <CheckCircle2 className="h-3.5 w-3.5 text-emerald-600" /> No duplicate rows
      </li>
    </ul>
  </div>,
];

const INNER_CARDS: React.ReactNode[] = [
  // SPIR type
  <div key="type" className={innerCard}>
    <div className="flex items-center justify-between">
      <span className="text-[11px] font-medium text-slate-500">SPIR type</span>
      <span className="flex items-center gap-1 text-[11px] font-semibold text-cdc-navy">
        <CheckCircle2 className="h-3.5 w-3.5" /> Ticked
      </span>
    </div>
    <p className="mt-1.5 text-[13px] font-semibold text-slate-800">Normal Operating Spares</p>
  </div>,

  // Revision compare
  <div key="rev" className={innerCard}>
    <div className="mb-2 flex items-center justify-between">
      <span className="text-[13px] font-semibold text-slate-800">Revision compare</span>
      <GitCompare className="h-4 w-4 text-cdc-navy" />
    </div>
    <div className="flex items-center gap-1.5 text-[11px]">
      <span className="rounded-full bg-slate-100 px-2 py-0.5 text-slate-600">Rev B</span>
      <span className="text-cdc-navy">→</span>
      <span className="rounded-full bg-cdc-gold/25 px-2 py-0.5 font-medium text-slate-800">Rev C</span>
      <span className="ml-auto font-medium text-cdc-navy">14 Δ</span>
    </div>
  </div>,

  // BOM export
  <div key="export" className={innerCard}>
    <div className="flex items-center gap-2.5">
      <div className="flex h-8 w-8 items-center justify-center rounded-xl bg-emerald-50 text-emerald-600">
        <Download className="h-4 w-4" />
      </div>
      <div>
        <p className="text-[13px] font-semibold text-slate-800">Bill of material</p>
        <p className="text-[11px] text-slate-500">Excel export ready</p>
      </div>
    </div>
  </div>,

  // Planning plant
  <div key="plant" className={innerCard}>
    <div className="flex items-center gap-2.5">
      <div className="flex h-8 w-8 items-center justify-center rounded-xl bg-cdc-navy/10 text-cdc-navy">
        <Factory className="h-4 w-4" />
      </div>
      <div>
        <p className="text-[13px] font-semibold text-slate-800">Planning plant</p>
        <p className="text-[11px] text-slate-500">Assigned on upload</p>
      </div>
    </div>
  </div>,

  // Currency
  <div key="fx" className={innerCard}>
    <div className="flex items-center justify-between">
      <span className="text-[13px] font-semibold text-slate-800">Currency</span>
      <Coins className="h-4 w-4 text-cdc-gold" />
    </div>
    <p className="mt-1.5 font-mono text-[11px] text-slate-600">
      QAR <span className="text-cdc-navy">→</span> USD · rate snapshot
    </p>
  </div>,

  // Batch upload
  <div key="batch" className={innerCard}>
    <div className="flex items-center justify-between">
      <span className="text-[13px] font-semibold text-slate-800">Batch upload</span>
      <UploadCloud className="h-4 w-4 text-cdc-navy" />
    </div>
    <div className="mt-2 h-1.5 w-full rounded-full bg-slate-100">
      <div className="h-1.5 w-3/5 rounded-full bg-cdc-navy" />
    </div>
    <p className="mt-1 text-[11px] text-slate-500">18 of 30 files processed</p>
  </div>,
];

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
      const data = await res.json().catch(() => ({}));
      if (res.status === 201) {
        setSubmitted(true);
      } else if (res.status === 404) {
        setError(data.detail ?? "No matching account found.");
      } else {
        setError(data.detail ?? "Could not submit request. Please try again or contact your administrator.");
      }
    } catch {
      setError("Could not connect to the server.");
    } finally {
      setLoading(false);
    }
  }

  if (submitted) {
    return (
      <div className="space-y-4 py-2 text-center">
        <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-emerald-50">
          <CheckCircle2 className="h-6 w-6 text-emerald-600" />
        </div>
        <div>
          <h3 className="text-base font-bold text-slate-900">Request submitted</h3>
          <p className="mt-1 text-sm text-slate-500">
            Your administrator will review it. You&apos;ll be notified once your access is restored.
          </p>
        </div>
        <button onClick={onBack} className="text-sm font-semibold text-cdc-navy hover:underline">
          Back to sign in
        </button>
      </div>
    );
  }

  return (
    <>
      <button
        type="button"
        onClick={onBack}
        className="mb-3 flex items-center gap-1.5 text-xs font-medium text-slate-500 transition-colors hover:text-slate-800"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        Back to sign in
      </button>
      <h2 className="text-lg font-bold text-slate-900">Reset password</h2>
      <p className="mb-5 mt-1 text-sm text-slate-500">Send a request to your administrator to reset your access.</p>

      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label htmlFor="fp-username" className={labelCls}>
            Username <span className="text-red-500">*</span>
          </label>
          <div className="relative">
            <UserRound className={iconCls} />
            <input id="fp-username" name="username" type="text" required autoComplete="username" placeholder="Your username" className={inputCls} />
          </div>
        </div>

        <div>
          <label htmlFor="fp-email" className={labelCls}>
            Email <span className="font-normal text-slate-400">(optional)</span>
          </label>
          <div className="relative">
            <Mail className={iconCls} />
            <input id="fp-email" name="email" type="email" autoComplete="email" placeholder="your@email.com" className={inputCls} />
          </div>
        </div>

        <div>
          <label htmlFor="fp-reason" className={labelCls}>
            Reason <span className="font-normal text-slate-400">(optional)</span>
          </label>
          <textarea
            id="fp-reason"
            name="reason"
            rows={2}
            maxLength={500}
            placeholder="e.g. Forgot password, account locked…"
            className="w-full resize-none rounded-xl border border-slate-200/80 bg-slate-50 px-4 py-2.5 text-sm text-slate-900 placeholder:text-slate-400 transition-all focus:border-cdc-navy/40 focus:bg-white focus:outline-none focus:ring-4 focus:ring-cdc-navy/10"
          />
        </div>

        {error && <ErrorBox message={error} />}

        <PrimaryButton loading={loading} loadingText="Submitting…">
          Submit request
        </PrimaryButton>
      </form>
    </>
  );
}

// ─── Small shared bits ────────────────────────────────────────────────────────

function ErrorBox({ message }: { message: string }) {
  return (
    <div role="alert" className="flex items-start gap-2.5 rounded-xl bg-red-50 p-3 text-sm text-red-700">
      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
      <span>{message}</span>
    </div>
  );
}

function PrimaryButton({
  loading,
  loadingText,
  children,
}: {
  loading: boolean;
  loadingText: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="submit"
      disabled={loading}
      className="group mt-1 flex h-12 w-full items-center justify-center gap-2 rounded-full bg-cdc-navy text-[15px] font-semibold text-white shadow-[0_10px_25px_-10px_rgba(0,58,137,0.6)] transition-all duration-200 hover:-translate-y-0.5 hover:bg-cdc-navy-dark active:translate-y-0 disabled:translate-y-0 disabled:opacity-60"
    >
      {loading ? (
        <>
          <Loader2 className="h-4 w-4 animate-spin" />
          {loadingText}
        </>
      ) : (
        <>
          {children}
          <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
        </>
      )}
    </button>
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
      clearCompareSession();
      resetCompareMemory();
      saveToken(access_token);

      try {
        const meRes = await fetch(`${API_URL}/api/me`, {
          headers: { Authorization: `Bearer ${access_token}` },
        });
        if (meRes.ok) {
          const meData = await meRes.json();
          saveRole(meData.role ?? "user");
          saveBranchId(meData.branch_id ?? null);
        } else {
          saveRole("user");
          saveBranchId(null);
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
    <div className={`${jakarta.className} relative min-h-screen overflow-hidden bg-white text-slate-900 antialiased`}>
      {/* Faint CDC-palette glows on a white canvas */}
      <div aria-hidden="true" className="pointer-events-none fixed inset-0">
        <div className="absolute -left-32 -top-40 h-[32rem] w-[32rem] rounded-full bg-cdc-gold/15 blur-[120px]" />
        <div className="absolute -right-40 -top-32 h-[36rem] w-[36rem] rounded-full bg-cdc-navy/[0.07] blur-[130px]" />
        <div className="absolute -bottom-48 right-1/4 h-[34rem] w-[34rem] rounded-full bg-cdc-gold/10 blur-[140px]" />
        <div className="absolute left-1/2 top-1/2 h-[40rem] w-[40rem] -translate-x-1/2 -translate-y-1/2 rounded-full bg-[radial-gradient(circle,rgba(0,58,137,0.05)_0%,rgba(255,255,255,0)_70%)]" />
      </div>

      {/* Breadcrumb brand */}
      <div className="absolute left-8 top-6 z-20 flex select-none items-center gap-1.5 text-[15px]">
        <span className="font-bold lowercase text-slate-900">cdc int</span>
        <span className="text-slate-300">/</span>
        <span className="font-medium lowercase text-slate-500">spir tool</span>
      </div>

      <PeripheralCards />

      <main className="relative z-10 flex min-h-screen items-center justify-center px-4 py-16">
        <div className="flex w-full max-w-[440px] flex-col items-center text-center motion-safe:animate-cdc-rise">
          <BrandMark />

          <h1 className="mb-7 text-[30px] font-bold leading-[38px] tracking-[-0.02em] text-slate-900 sm:text-[34px] sm:leading-[42px]">
            Spare Parts Interchangeability Records
          </h1>

          <div className="w-full rounded-[28px] border border-slate-200/80 bg-white/90 p-6 text-left shadow-[0_20px_50px_-12px_rgba(15,23,42,0.08),0_1px_3px_0_rgba(0,0,0,0.02)] backdrop-blur-sm">
            {forgotMode ? (
              <ForgotPasswordForm onBack={() => setForgotMode(false)} />
            ) : (
              <form onSubmit={handleSubmit} className="space-y-4">
                <div>
                  <label htmlFor="username" className={labelCls}>
                    Username or Email
                  </label>
                  <div className="relative">
                    <UserRound className={iconCls} />
                    <input
                      id="username"
                      name="username"
                      type="text"
                      autoComplete="username"
                      required
                      placeholder="Enter username or email"
                      className={inputCls}
                    />
                  </div>
                </div>

                <div>
                  <div className="mb-1.5 flex items-center justify-between">
                    <label htmlFor="password" className="text-xs font-semibold text-slate-800">
                      Password
                    </label>
                    <button
                      type="button"
                      onClick={() => setForgotMode(true)}
                      className="text-xs font-medium text-cdc-navy hover:underline"
                    >
                      Forgot password?
                    </button>
                  </div>
                  <div className="relative">
                    <Lock className={iconCls} />
                    <input
                      id="password"
                      name="password"
                      type={showPass ? "text" : "password"}
                      autoComplete="current-password"
                      required
                      placeholder="Enter your password"
                      className={`${inputCls} pr-11`}
                    />
                    <button
                      type="button"
                      onClick={() => setShowPass((p) => !p)}
                      aria-label={showPass ? "Hide password" : "Show password"}
                      className="absolute right-3.5 top-1/2 -translate-y-1/2 text-slate-400 transition-colors hover:text-slate-700"
                    >
                      {showPass ? <EyeOff className="h-[18px] w-[18px]" /> : <Eye className="h-[18px] w-[18px]" />}
                    </button>
                  </div>
                </div>

                <label className="flex cursor-pointer select-none items-center gap-2 pt-0.5">
                  <input
                    type="checkbox"
                    checked={remember}
                    onChange={(e) => setRemember(e.target.checked)}
                    className="h-4 w-4 rounded border-slate-300 accent-cdc-navy"
                  />
                  <span className="text-xs font-medium text-slate-600">Remember this device</span>
                </label>

                {error && <ErrorBox message={error} />}

                <PrimaryButton loading={loading} loadingText="Signing in…">
                  Sign In
                </PrimaryButton>
              </form>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}
