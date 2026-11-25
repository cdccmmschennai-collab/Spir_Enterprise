"use client";

import * as React from "react";
import { usePathname } from "next/navigation";

// Remembers the previous in-app route, so "Back" buttons can tell whether
// there is somewhere inside the app to return to. (document.referrer does not
// change on client-side navigation, so it can't be used for this.) Mounted
// once in app/layout.tsx, which survives route changes.

const PrevPathContext = React.createContext<string | null>(null);

export function NavHistoryProvider({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const current = React.useRef<string | null>(null);
  const [previous, setPrevious] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (current.current !== null && current.current !== pathname) setPrevious(current.current);
    current.current = pathname;
  }, [pathname]);

  return <PrevPathContext.Provider value={previous}>{children}</PrevPathContext.Provider>;
}

/** The route visited before the current one in this tab, or null (direct visit / reload). */
export function usePreviousPath() {
  return React.useContext(PrevPathContext);
}
