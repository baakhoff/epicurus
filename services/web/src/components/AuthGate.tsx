/**
 * The auth gate (#969): the one place that decides whether the app mounts at all.
 *
 * It sits above the router and the shell, so nothing that fetches data exists until the
 * session check has answered — a signed-out browser sees the sign-in screen, not a burst of
 * refused requests behind it. With sign-in off (`mode: "none"`, the default) the check answers
 * "go ahead" and the shell renders exactly as it always did. The decisions themselves live in
 * the auth store (`src/stores/auth.ts`); this only renders them.
 */
import { useEffect, useState, type ReactNode } from "react";

import { EpsilonMark } from "@/components/Logo";
import { SignInScreen } from "@/screens/SignInScreen";
import { useAuth } from "@/stores/auth";

/** How long the boot check may run before it earns a visible mark. A healthy core answers well
 *  inside this, so the common case shows the canvas for a blink and then the app — no flash of
 *  a loading state for a check that never needed one. */
const BOOT_MARK_DELAY_MS = 400;

function BootScreen() {
  const [showMark, setShowMark] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => setShowMark(true), BOOT_MARK_DELAY_MS);
    return () => clearTimeout(timer);
  }, []);
  return (
    <div role="status" aria-label="Opening epicurus" className="flex h-full items-center justify-center">
      {showMark && <EpsilonMark size={44} draw />}
    </div>
  );
}

export function AuthGate({ children }: { children: ReactNode }) {
  const phase = useAuth((s) => s.phase);

  useEffect(() => {
    void useAuth.getState().boot();
  }, []);

  if (phase === "app") return <>{children}</>;
  if (phase === "signed-out" || phase === "redirecting") return <SignInScreen />;
  return <BootScreen />;
}
