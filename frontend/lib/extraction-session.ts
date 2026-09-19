const STORAGE_KEY_PREFIX = "spir_extraction_session";
const SESSION_TTL = 24 * 60 * 60 * 1000;

export interface ExtractionSession {
  status: "loading" | "complete";
  filename: string;
  savedAt: number;
  // Bytes, when known. The File object itself is never persisted.
  size?: number;
  job_id?: string;
  // "uploading": the browser was writing the file directly to storage when
  // this was saved. That transfer cannot survive a navigation (the File is
  // gone), so on restore it is cancelled server-side instead of polled.
  phase?: "uploading" | "processing";
  result?: unknown;
  // The user chose "Start a new extraction" while this one was still running.
  // The job carries on server-side (and lands in History), but the page must
  // not restore it on the next mount — otherwise the user could never leave it.
  dismissed?: boolean;
}

// Decode JWT to get the user's stable DB UUID (uid claim), falling back to
// the username (sub claim) in legacy/no-DB mode. Keying by uid is consistent
// with how the backend scopes extraction_history records.
function getUserId(): string {
  if (typeof window === "undefined") return "";
  try {
    const token = localStorage.getItem("token");
    if (!token) return "";
    const parts = token.split(".");
    if (parts.length !== 3) return "";
    const b64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const payload = JSON.parse(atob(b64)) as Record<string, unknown>;
    return String(payload.uid ?? payload.sub ?? "");
  } catch {
    return "";
  }
}

function getStorageKey(): string {
  const uid = getUserId();
  return uid ? `${STORAGE_KEY_PREFIX}-${uid}` : STORAGE_KEY_PREFIX;
}

export function saveSession(session: ExtractionSession): void {
  try {
    localStorage.setItem(getStorageKey(), JSON.stringify(session));
  } catch {}
}

export function loadSession(): ExtractionSession | null {
  try {
    const key = getStorageKey();
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const session = JSON.parse(raw) as ExtractionSession;
    if (Date.now() - session.savedAt > SESSION_TTL) {
      localStorage.removeItem(key);
      return null;
    }
    return session;
  } catch {
    return null;
  }
}

export function clearSession(): void {
  try {
    localStorage.removeItem(getStorageKey());
  } catch {}
}

// Leave the current session behind without deleting it: the job_id (if any)
// stays on record, but loadSession() callers treat it as intentionally left.
// A later saveSession() for a new extraction simply replaces it.
export function dismissSession(): void {
  const session = loadSession();
  if (session && !session.dismissed) saveSession({ ...session, dismissed: true });
}
