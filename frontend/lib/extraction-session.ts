const STORAGE_KEY = "spir_extraction_session";
const SESSION_TTL = 24 * 60 * 60 * 1000;

export interface ExtractionSession {
  status: "loading" | "complete";
  filename: string;
  savedAt: number;
  result?: unknown;
}

export function saveSession(session: ExtractionSession): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(session));
  } catch {}
}

export function loadSession(): ExtractionSession | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const session = JSON.parse(raw) as ExtractionSession;
    if (Date.now() - session.savedAt > SESSION_TTL) {
      clearSession();
      return null;
    }
    return session;
  } catch {
    return null;
  }
}

export function clearSession(): void {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {}
}
