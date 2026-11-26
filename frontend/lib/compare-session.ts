import { getUserId } from "@/lib/extraction-session";

// Keeps the SPIR Comparison page's last result across tab navigation, the same
// way extraction-session.ts does for the Extraction page: user-scoped
// localStorage key, 24 h TTL, cleared on logout / login / "New Comparison".
const STORAGE_KEY_PREFIX = "spir_compare_session";
const SESSION_TTL = 24 * 60 * 60 * 1000;

export interface CompareSession {
  savedAt: number;
  result: unknown;
}

function getStorageKey(): string {
  const uid = getUserId();
  return uid ? `${STORAGE_KEY_PREFIX}-${uid}` : STORAGE_KEY_PREFIX;
}

export function saveCompareSession(result: unknown): void {
  try {
    localStorage.setItem(getStorageKey(), JSON.stringify({ savedAt: Date.now(), result }));
  } catch {}
}

export function loadCompareSession(): CompareSession | null {
  try {
    const key = getStorageKey();
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const session = JSON.parse(raw) as CompareSession;
    if (!session?.result || Date.now() - session.savedAt > SESSION_TTL) {
      localStorage.removeItem(key);
      return null;
    }
    return session;
  } catch {
    return null;
  }
}

export function clearCompareSession(): void {
  try {
    localStorage.removeItem(getStorageKey());
  } catch {}
}

// In-memory state that must outlive the page component but not a reload: the
// picked File objects (cannot be serialised) and a comparison still running.
// Module state survives client-side navigation, so on return the page shows
// the same files and re-attaches to the same request instead of losing it.
export type CompareOutcome =
  | { kind: "result"; result: unknown }
  | { kind: "error"; message: string }
  | { kind: "aborted" };

export interface PendingCompare {
  controller: AbortController;
  promise: Promise<CompareOutcome>;
}

export const compareMemory: {
  source: File | null;
  comparison: File | null;
  pending: PendingCompare | null;
} = { source: null, comparison: null, pending: null };

export function resetCompareMemory(): void {
  compareMemory.pending?.controller.abort();
  compareMemory.source = null;
  compareMemory.comparison = null;
  compareMemory.pending = null;
}
