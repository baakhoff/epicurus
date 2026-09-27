/**
 * The sign-in screen (#969) — what a browser without a session sees when the operator has
 * turned sign-in on (`AUTH_MODE=oidc`).
 *
 * It replaces the whole shell rather than floating over it: nothing behind it has been fetched
 * (the gate never mounted the app), or everything that was has just been dropped (the session
 * ended). One action — "Sign in with {provider}" — hands the window to the core's login route
 * with a `next` that brings the browser back to exactly where it was: the screen a 401
 * interrupted, a share waiting at `/?share=1`, a notification's deep link.
 *
 * Built from the shell's own parts, in its own language: the ε mark, a serif heading, the
 * primary Button, the same bordered notices Settings uses. On a wide screen it is a centred
 * card; on a phone it fills the height, clears the notch and home indicator (`pt-safe` /
 * `pb-safe`), and puts a tall, full-width button where a thumb rests.
 */
import { useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, CircleAlert, Info, LogIn } from "lucide-react";
import { useEffect, type ReactNode } from "react";

import { EpsilonMark } from "@/components/Logo";
import { Button, cn } from "@/components/ui";
import { authErrorMessage } from "@/lib/auth";
import { useAuth, type AuthState } from "@/stores/auth";

type NoticeTone = "error" | "ok" | "info";

function Notice({ tone, children }: { tone: NoticeTone; children: ReactNode }) {
  const Icon = tone === "error" ? CircleAlert : tone === "ok" ? CheckCircle2 : Info;
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      className={cn(
        "flex items-start gap-2 rounded-(--radius-field) border px-3 py-2 text-left text-sm",
        tone === "error" && "border-danger/40 bg-danger/5 text-danger",
        tone === "ok" && "border-ok/40 bg-ok/5 text-ok",
        tone === "info" && "border-edge bg-surface-2 text-ink-dim",
      )}
    >
      <Icon size={15} className="mt-0.5 shrink-0" />
      <p className="min-w-0">{children}</p>
    </div>
  );
}

/** At most one notice: a failure outranks a note, and of the notes the most specific wins. */
function notice(
  state: Pick<AuthState, "error" | "unreachable" | "reason" | "loopGuarded">,
): { tone: NoticeTone; text: string } | null {
  if (state.error !== null) return { tone: "error", text: authErrorMessage(state.error) };
  if (state.unreachable) {
    return {
      tone: "error",
      text: "Can't reach epicurus right now. Check your connection, then try again.",
    };
  }
  if (state.reason === "signed-out") return { tone: "ok", text: "You're signed out." };
  if (state.loopGuarded) {
    return { tone: "info", text: "Signing in didn't finish, so you're still signed out." };
  }
  if (state.reason === "expired") {
    return {
      tone: "info",
      text: "Your session has ended. Sign in again to pick up where you left off.",
    };
  }
  return null;
}

export function SignInScreen() {
  const phase = useAuth((s) => s.phase);
  const providerName = useAuth((s) => s.session?.provider_name?.trim() || null);
  const error = useAuth((s) => s.error);
  const unreachable = useAuth((s) => s.unreachable);
  const reason = useAuth((s) => s.reason);
  const loopGuarded = useAuth((s) => s.loopGuarded);
  const queryClient = useQueryClient();

  // Nothing the app fetched outlives the session it was fetched under: a session that ended,
  // or a sign-out, drops the whole cache before anyone could be shown it again.
  useEffect(() => {
    queryClient.clear();
  }, [queryClient]);

  useEffect(() => {
    // Back from the provider's page restores this tab from the back-forward cache exactly as it
    // was left — mid-redirect, button spinning. Un-freeze it.
    const onPageShow = (event: PageTransitionEvent) => {
      if (event.persisted) useAuth.getState().resumeFromHistory();
    };
    // Signed in from another tab meanwhile? Returning to this one asks once, quietly — a
    // re-check never redirects on its own.
    const onVisible = () => {
      if (document.visibilityState === "visible" && useAuth.getState().phase === "signed-out") {
        void useAuth.getState().refresh();
      }
    };
    window.addEventListener("pageshow", onPageShow);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.removeEventListener("pageshow", onPageShow);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);

  const redirecting = phase === "redirecting";
  const shown = notice({ error, unreachable, reason, loopGuarded });
  const label = providerName ? `Sign in with ${providerName}` : "Sign in";
  const busyLabel = providerName ? `Taking you to ${providerName}…` : "Taking you to sign in…";

  return (
    <main
      aria-labelledby="sign-in-heading"
      className="flex h-full flex-col overflow-y-auto pt-safe pb-safe sm:items-center sm:justify-center"
    >
      <div
        className={cn(
          "flex w-full flex-1 flex-col px-6 py-8",
          "sm:max-w-sm sm:flex-none sm:rounded-(--radius-card) sm:border sm:border-edge sm:bg-surface sm:p-7 sm:shadow-(--ep-shadow)",
        )}
      >
        <div className="flex flex-1 flex-col items-center justify-center gap-3 text-center sm:flex-none">
          <EpsilonMark size={44} draw />
          <h1 id="sign-in-heading" className="font-serif text-xl text-ink">
            Sign in to epicurus
          </h1>
          <p className="max-w-xs text-sm leading-relaxed text-ink-dim">
            This epicurus is private. Sign in to reach your conversations, memory and modules.
          </p>
        </div>
        <div className="mt-8 flex flex-col gap-3 sm:mt-6">
          {shown && <Notice tone={shown.tone}>{shown.text}</Notice>}
          <Button
            variant="primary"
            busy={redirecting}
            onClick={() => void useAuth.getState().signIn()}
            className="h-12 w-full sm:h-11"
          >
            {!redirecting && <LogIn size={17} />}
            <span className="text-base sm:text-sm">{redirecting ? busyLabel : label}</span>
          </Button>
        </div>
      </div>
    </main>
  );
}
