// Direct browser -> MinIO upload client (Phase 3D).
//
// For large workbooks the API no longer receives the file body. Flow:
//
//   POST /api/uploads/initiate            -> { mode: "api" }     caller keeps its existing upload code
//                                         -> { mode: "direct", parts: [{ part_number, url }], ... }
//   PUT <presigned url>  (x part_count, straight to MinIO, a few in parallel, per-part retry)
//   POST /api/uploads/{job}/{idx}/parts   -> fresh URLs when one expired
//   POST /api/uploads/{job}/{idx}/complete -> 202 { status: "queued", job_id, queue }  (server verified)
//   DELETE /api/uploads/{job}/{idx}       -> cancel
//
// Nothing here knows a bucket, a key or a credential: the server hands out
// single-part, PUT-only, expiring URLs and decides everything else.

import { authHeaders } from "@/lib/auth";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const PART_CONCURRENCY = 3;
const PART_MAX_ATTEMPTS = 4;
const COMPLETE_MAX_ROUNDS = 3;

interface PresignedPart {
  part_number: number;
  url: string;
}

interface DirectPlan {
  mode: "direct";
  job_id: string;
  file_idx: number;
  filename: string;
  size: number;
  part_size: number;
  part_count: number;
  parts: PresignedPart[];
  url_expires_in: number;
  queue: string;
}

export interface QueuedResponse {
  status: "queued";
  job_id: string;
  file_idx: number;
  filename: string;
  size_mb: number;
  queue: string;
}

export type DirectUploadOutcome =
  | { kind: "api" }                                   // not a direct-upload case — use the classic endpoint
  | { kind: "queued"; response: QueuedResponse }      // uploaded, verified and queued for a worker
  | { kind: "cancelled" }
  | { kind: "error"; status: number; message: string };

export interface DirectUploadOptions {
  // Batch slot registered via /api/batch/register; omit for the single-file page.
  jobId?: string;
  fileIdx?: number;
  onProgress?: (uploadedBytes: number, totalBytes: number) => void;
  onPhase?: (phase: "uploading" | "finalizing") => void;
  // Reveals the job id as soon as the server created it (single-file page).
  onJob?: (jobId: string, fileIdx: number) => void;
  signal?: AbortSignal;
}

export class DirectUploadCancelled extends Error {
  constructor() {
    super("upload cancelled");
    this.name = "DirectUploadCancelled";
  }
}

class PartUploadError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
    this.name = "PartUploadError";
  }
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => { clearTimeout(t); reject(new DirectUploadCancelled()); }, { once: true });
  });
}

// XMLHttpRequest rather than fetch: it is the only browser API with upload
// progress events. The request carries no credentials or custom headers —
// the presigned URL is the whole authorisation.
function putPart(url: string, blob: Blob, onLoaded: (bytes: number) => void, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new DirectUploadCancelled());
    const xhr = new XMLHttpRequest();
    const onAbort = () => xhr.abort();
    signal?.addEventListener("abort", onAbort, { once: true });
    xhr.open("PUT", url, true);
    xhr.upload.onprogress = (e) => onLoaded(e.loaded);
    xhr.onload = () => {
      signal?.removeEventListener("abort", onAbort);
      if (xhr.status >= 200 && xhr.status < 300) { onLoaded(blob.size); resolve(); }
      else reject(new PartUploadError(xhr.status, `part upload failed (${xhr.status})`));
    };
    xhr.onerror = () => { signal?.removeEventListener("abort", onAbort); reject(new PartUploadError(0, "network error")); };
    xhr.onabort = () => { signal?.removeEventListener("abort", onAbort); reject(new DirectUploadCancelled()); };
    xhr.send(blob);
  });
}

async function apiJson(path: string, init: RequestInit): Promise<{ status: number; body: Record<string, unknown> }> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: { ...authHeaders(), "Content-Type": "application/json", ...(init.headers ?? {}) },
  });
  const body = (await res.json().catch(() => ({}))) as Record<string, unknown>;
  return { status: res.status, body };
}

async function presignParts(jobId: string, fileIdx: number, numbers: number[]): Promise<Map<number, string>> {
  const { status, body } = await apiJson(`/api/uploads/${jobId}/${fileIdx}/parts`, {
    method: "POST",
    body: JSON.stringify({ part_numbers: numbers }),
  });
  if (status !== 200) throw new PartUploadError(status, String(body.detail ?? `could not refresh upload URLs (${status})`));
  const out = new Map<number, string>();
  for (const p of (body.parts as PresignedPart[]) ?? []) out.set(p.part_number, p.url);
  return out;
}

// Upload the given part numbers with bounded concurrency. Each part retries
// with backoff; a 403 (expired URL) fetches a fresh URL from the API first.
async function uploadParts(
  plan: DirectPlan,
  file: File,
  numbers: number[],
  urls: Map<number, string>,
  loaded: Map<number, number>,
  report: () => void,
  signal?: AbortSignal,
): Promise<void> {
  const queue = [...numbers];
  const worker = async () => {
    for (;;) {
      const n = queue.shift();
      if (n === undefined) return;
      const start = (n - 1) * plan.part_size;
      const blob = file.slice(start, Math.min(start + plan.part_size, file.size));
      for (let attempt = 1; ; attempt++) {
        if (signal?.aborted) throw new DirectUploadCancelled();
        try {
          let url = urls.get(n);
          if (!url) { url = (await presignParts(plan.job_id, plan.file_idx, [n])).get(n); if (url) urls.set(n, url); }
          if (!url) throw new PartUploadError(0, `no URL for part ${n}`);
          await putPart(url, blob, (b) => { loaded.set(n, b); report(); }, signal);
          break;
        } catch (err) {
          if (err instanceof DirectUploadCancelled) throw err;
          loaded.set(n, 0);
          report();
          if (attempt >= PART_MAX_ATTEMPTS) throw err;
          if (err instanceof PartUploadError && err.status === 403) urls.delete(n); // expired — re-sign on retry
          await sleep(500 * 2 ** (attempt - 1), signal);
        }
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(PART_CONCURRENCY, queue.length) }, worker));
}

async function abortUpload(jobId: string, fileIdx: number): Promise<void> {
  try { await fetch(`${API_URL}/api/uploads/${jobId}/${fileIdx}`, { method: "DELETE", headers: authHeaders() }); } catch {}
}

/**
 * Ask the server how `file` should be uploaded and, if it says "direct",
 * carry the whole browser -> MinIO -> complete flow out. Resolves with
 * { kind: "api" } when the caller should use its existing upload request.
 */
export async function directUpload(file: File, opts: DirectUploadOptions = {}): Promise<DirectUploadOutcome> {
  const { signal } = opts;
  let init: { status: number; body: Record<string, unknown> };
  try {
    init = await apiJson("/api/uploads/initiate", {
      method: "POST",
      body: JSON.stringify({
        filename: file.name,
        size: file.size,
        ...(opts.jobId !== undefined ? { job_id: opts.jobId, file_idx: opts.fileIdx ?? 0 } : {}),
      }),
    });
  } catch {
    return { kind: "error", status: 0, message: "Could not reach the server. Is the backend running?" };
  }
  if (init.status !== 200) {
    return { kind: "error", status: init.status, message: String(init.body.detail ?? `Upload could not start (${init.status})`) };
  }
  if (init.body.mode !== "direct") return { kind: "api" };

  const plan = init.body as unknown as DirectPlan;
  opts.onJob?.(plan.job_id, plan.file_idx);
  opts.onPhase?.("uploading");

  const urls = new Map<number, string>(plan.parts.map((p) => [p.part_number, p.url]));
  const loaded = new Map<number, number>();
  const report = () => {
    let sum = 0;
    loaded.forEach((b) => { sum += b; });
    opts.onProgress?.(Math.min(sum, file.size), file.size);
  };

  try {
    let numbers = Array.from({ length: plan.part_count }, (_, i) => i + 1);
    for (let round = 1; ; round++) {
      await uploadParts(plan, file, numbers, urls, loaded, report, signal);
      if (signal?.aborted) throw new DirectUploadCancelled();
      opts.onPhase?.("finalizing");
      const done = await apiJson(`/api/uploads/${plan.job_id}/${plan.file_idx}/complete`, { method: "POST" });
      if (done.status === 202) return { kind: "queued", response: done.body as unknown as QueuedResponse };
      // 409 + missing_parts: MinIO does not hold every part — retry just those.
      const missing = (done.body.missing_parts as number[] | undefined) ?? [];
      const wrong = (done.body.wrong_parts as number[] | undefined) ?? [];
      const redo = Array.from(new Set(missing.concat(wrong)));
      if (done.status === 409 && redo.length > 0 && round < COMPLETE_MAX_ROUNDS) {
        numbers = redo;
        redo.forEach((n) => { loaded.set(n, 0); urls.delete(n); });
        opts.onPhase?.("uploading");
        continue;
      }
      if (done.status === 503 && round < COMPLETE_MAX_ROUNDS) { await sleep(2000, signal); numbers = []; continue; }
      await abortUpload(plan.job_id, plan.file_idx);
      return { kind: "error", status: done.status, message: String(done.body.detail ?? `Upload could not be completed (${done.status})`) };
    }
  } catch (err) {
    await abortUpload(plan.job_id, plan.file_idx);
    if (err instanceof DirectUploadCancelled) return { kind: "cancelled" };
    const status = err instanceof PartUploadError ? err.status : 0;
    const message = status === 0
      ? "Upload interrupted — check your connection and try again."
      : `Upload failed (${status}). Please try again.`;
    return { kind: "error", status, message };
  }
}

/** Cancel an in-flight direct upload the page knows only by job id (e.g. after a reload). */
export async function cancelDirectUpload(jobId: string, fileIdx = 0): Promise<void> {
  await abortUpload(jobId, fileIdx);
}
