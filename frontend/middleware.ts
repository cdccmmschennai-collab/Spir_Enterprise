import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

export function middleware(request: NextRequest) {
  const token =
    request.cookies.get("access_token")?.value ||
    request.cookies.get("token")?.value;

  const role = request.cookies.get("role")?.value;

  const { pathname } = request.nextUrl;

  // Public routes (no auth needed)
  const isPublic =
    pathname === "/login" ||
    pathname.startsWith("/_next") ||
    pathname.startsWith("/favicon") ||
    pathname.startsWith("/api") ||
    /\.(?:jpg|jpeg|png|svg|gif|webp|ico|css|js|woff2?)$/i.test(pathname);

  // If NOT logged in → redirect to login
  if (!token && !isPublic) {
    return NextResponse.redirect(new URL("/login", request.url));
  }

  // If logged in → prevent going back to login
  if (token && pathname === "/login") {
    return NextResponse.redirect(new URL("/extraction", request.url));
  }

  // Admin route guard — only super_admin and branch_admin may access /admin
  const ADMIN_ROLES = ["admin", "super_admin", "branch_admin"]; // "admin" kept for tokens issued before migration
  if (token && pathname.startsWith("/admin") && !ADMIN_ROLES.includes(role ?? "")) {
    return NextResponse.redirect(new URL("/extraction", request.url));
  }

  return NextResponse.next();
}

export const config = {
  matcher: [
    // Run middleware on all routes EXCEPT Next.js internals and static file extensions
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:jpg|jpeg|png|svg|gif|webp|ico|css|js|woff2?)$).*)",
  ],
};
