/**
 * Settings → Account (#969): who is signed in, through which provider, and Sign out.
 *
 * Rendered only when sign-in is on (`mode: "oidc"`) and a session exists — with sign-in off
 * there is no account to show, so the card is simply absent and Settings reads exactly as it
 * did. It reads the session through its own query rather than the gate's copy, so it refreshes
 * with everything else when the core comes back from an outage (the connection watch
 * invalidates the whole cache on recovery).
 *
 * Sign out asks the core to end the session (`POST /platform/v1/auth/logout`) and, once it has,
 * the gate shows the signed-out screen — with auto-redirect held off in this tab, so signing
 * out is not immediately undone by a provider that still remembers you.
 */
import { useMutation, useQuery } from "@tanstack/react-query";
import { LogOut } from "lucide-react";

import { Button, Card } from "@/components/ui";
import { api } from "@/lib/api";
import { useAuth } from "@/stores/auth";

export function AccountCard() {
  const session = useQuery({
    queryKey: ["auth-session"],
    queryFn: () => api.authSession(),
    staleTime: 30_000,
  });
  const signOut = useMutation({ mutationFn: () => useAuth.getState().signOut() });

  const data = session.data;
  if (!data || data.mode === "none" || !data.signed_in) return null;

  const name = data.user?.name?.trim() || null;
  const email = data.user?.email?.trim() || null;
  const subject = data.user?.subject ?? null;
  const provider = data.provider_name?.trim() || null;

  return (
    <Card>
      <h3 className="mb-2 font-serif text-base text-ink">Account</h3>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm break-words text-ink">
            {name || email ? (
              <>
                Signed in as <span className="font-medium">{name ?? email}</span>
                {name && email && <span className="text-ink-dim"> · {email}</span>}
              </>
            ) : (
              "Signed in"
            )}
          </p>
          {!name && !email && subject && (
            <p className="font-mono text-xs break-all text-ink-faint">account {subject}</p>
          )}
          <p className="text-xs text-ink-faint">
            {provider ? `through ${provider}` : "through your sign-in provider"}
          </p>
        </div>
        <Button
          variant="outline"
          busy={signOut.isPending}
          onClick={() => signOut.mutate()}
          className="w-full sm:w-auto"
        >
          <LogOut size={15} />
          Sign out
        </Button>
      </div>
      {signOut.isError && (
        <p className="mt-2 text-xs text-danger">
          Couldn't sign out — {(signOut.error as Error).message}. You're still signed in.
        </p>
      )}
    </Card>
  );
}
