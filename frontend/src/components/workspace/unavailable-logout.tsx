"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { buildLoginUrl } from "@/core/auth/types";

const COOKIE_NAMES = [
  "access_token",
  "csrf_token",
  "refresh_token",
] as const;

/**
 * Client-side cookie clearing helper.
 *
 * Sets each known auth cookie to expire in the past with a path of ``/``
 * so that the browser removes them immediately without requiring a
 * round-trip to the backend.  This is designed to work even when the
 * backend is completely unreachable (gateway_unavailable page).
 */
function clearAuthCookies(): void {
  COOKIE_NAMES.forEach((name) => {
    // Try common domain/path combinations to cover both root-path and
    // proxy-prefixed deployments.
    const paths = ["/", "/api"];
    paths.forEach((path) => {
      document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=${path};`;
    });
    // Also attempt to clear without explicit path (some browsers handle this).
    document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;`;
  });
}

/**
 * Logout button for the "Service temporarily unavailable" error page.
 *
 * When the backend is down, the standard ``<form action="/api/v1/auth/logout">``
 * cannot reach the server and leaves cookies intact.  This component clears
 * auth state entirely on the client side so the user can switch accounts
 * without depending on any backend endpoint.
 */
export function UnavailableLogout() {
  const router = useRouter();
  const [busy, setBusy] = useState(false);

  const handleLogout = () => {
    if (busy) return;
    setBusy(true);

    // 1. Clear auth cookies client-side (no backend dependency)
    clearAuthCookies();

    // 2. Clear sessionStorage keys that the reconnect mechanism relies on
    try {
      const storage = window.sessionStorage;
      const keysToRemove: string[] = [];
      for (let i = 0; i < storage.length; i++) {
        const key = storage.key(i);
        if (key && (key.startsWith("lg:stream:") || key.startsWith("lg:"))) {
          keysToRemove.push(key);
        }
      }
      keysToRemove.forEach((k) => storage.removeItem(k));
    } catch {
      // sessionStorage may be unavailable in some contexts
    }

    // 3. Navigate to login page so the user can sign in as a different account
    router.push(buildLoginUrl("/workspace"));
  };

  return (
    <button
      type="button"
      onClick={handleLogout}
      disabled={busy}
      className="text-muted-foreground hover:bg-muted rounded-md border px-4 py-2 text-sm disabled:opacity-50"
    >
      {busy ? "Clearing..." : "Logout & Reset"}
    </button>
  );
}
