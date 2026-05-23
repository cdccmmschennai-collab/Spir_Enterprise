const TOKEN_KEY = "token";
const COOKIE_NAME = "token";
const ROLE_KEY = "role";
const ROLE_COOKIE = "role";
const BRANCH_KEY = "branch_id";

/** Save token to both localStorage (for API calls) and a cookie (for middleware). */
export function saveToken(token: string): void {
  if (typeof window === "undefined") return;
  localStorage.setItem(TOKEN_KEY, token);
  document.cookie = `${COOKIE_NAME}=${token}; path=/; SameSite=Lax; max-age=${8 * 60 * 60}`;
}

/** Get token from localStorage. */
export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(TOKEN_KEY);
}

/** Clear token from both localStorage and cookie, and clear role + branch. */
export function clearToken(): void {
  if (typeof window === "undefined") return;
  localStorage.removeItem(TOKEN_KEY);
  document.cookie = `${COOKIE_NAME}=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT`;
  clearRole();
  clearBranchId();
}

/** Save user role to localStorage and a cookie (used by middleware for /admin guard). */
export function saveRole(role: string): void {
  if (typeof window === "undefined") return;
  localStorage.setItem(ROLE_KEY, role);
  document.cookie = `${ROLE_COOKIE}=${role}; path=/; SameSite=Lax; max-age=${8 * 60 * 60}`;
}

/** Get user role from localStorage. */
export function getRole(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(ROLE_KEY);
}

/** Clear role from localStorage and cookie. */
export function clearRole(): void {
  if (typeof window === "undefined") return;
  localStorage.removeItem(ROLE_KEY);
  document.cookie = `${ROLE_COOKIE}=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT`;
}

/** Save branch_id to localStorage. */
export function saveBranchId(branchId: string | null): void {
  if (typeof window === "undefined") return;
  if (branchId) {
    localStorage.setItem(BRANCH_KEY, branchId);
  } else {
    localStorage.removeItem(BRANCH_KEY);
  }
}

/** Get branch_id from localStorage. */
export function getBranchId(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(BRANCH_KEY);
}

/** Clear branch_id from localStorage. */
export function clearBranchId(): void {
  if (typeof window === "undefined") return;
  localStorage.removeItem(BRANCH_KEY);
}

/** Returns Authorization header object if token exists, otherwise empty. */
export function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}
