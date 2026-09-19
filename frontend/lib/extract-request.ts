// The classic single-file extraction request (POST /api/extract, multipart
// form, bearer auth) sent through XMLHttpRequest instead of fetch. The request
// on the wire is identical; XHR is simply the only browser API that reports
// upload progress, which lets the page show real bytes sent and move from
// "Uploading" to "Processing" the moment the body has been delivered.

import { authHeaders } from "@/lib/auth";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export interface ExtractRequestOptions {
  onProgress?: (loaded: number, total: number) => void;
  // Fires once the whole body has been sent — the server is now working.
  onSent?: () => void;
  signal?: AbortSignal;
}

export interface ExtractResponse {
  status: number;
  body: Record<string, unknown>;
}

export class ExtractRequestCancelled extends Error {
  constructor() {
    super("extraction request cancelled");
    this.name = "ExtractRequestCancelled";
  }
}

export class ExtractRequestNetworkError extends Error {
  constructor() {
    super("network error");
    this.name = "ExtractRequestNetworkError";
  }
}

export function postExtract(form: FormData, opts: ExtractRequestOptions = {}): Promise<ExtractResponse> {
  return new Promise((resolve, reject) => {
    const { signal } = opts;
    if (signal?.aborted) return reject(new ExtractRequestCancelled());

    const xhr = new XMLHttpRequest();
    const onAbort = () => xhr.abort();
    signal?.addEventListener("abort", onAbort, { once: true });
    const cleanup = () => signal?.removeEventListener("abort", onAbort);

    xhr.open("POST", `${API_URL}/api/extract`, true);
    for (const [k, v] of Object.entries(authHeaders())) xhr.setRequestHeader(k, v);

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) opts.onProgress?.(e.loaded, e.total);
    };
    xhr.upload.onload = () => opts.onSent?.();
    xhr.onload = () => {
      cleanup();
      let body: Record<string, unknown> = {};
      try { body = JSON.parse(xhr.responseText) as Record<string, unknown>; } catch {}
      resolve({ status: xhr.status, body });
    };
    xhr.onerror = () => { cleanup(); reject(new ExtractRequestNetworkError()); };
    xhr.onabort = () => { cleanup(); reject(new ExtractRequestCancelled()); };
    xhr.send(form);
  });
}
