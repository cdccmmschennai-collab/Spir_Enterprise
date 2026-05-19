const STORAGE_KEY_PREFIX = "spir_extraction_session";
const SESSION_TTL = 24 * 60 * 60 * 1000;

export interface ExtractionSession {
  status: "loading" | "complete";
  filename: string;
  savedAt: number;
  job_id?: string;
  result?: unknown;
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
