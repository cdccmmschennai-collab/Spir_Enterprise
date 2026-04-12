const TOKEN_KEY = "access_token";
const COOKIE_NAME = "access_token";

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

/** Clear token from both localStorage and cookie. */
export function clearToken(): void {
  if (typeof window === "undefined") return;
  localStorage.removeItem(TOKEN_KEY);
  document.cookie = `${COOKIE_NAME}=; path=/; max-age=0`;
}

/** Returns Authorization header object if token exists, otherwise empty. */
export function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}
