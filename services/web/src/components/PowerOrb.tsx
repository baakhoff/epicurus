/**
 * The power toggle (ADR-0005) — always visible in the top bar. Awake glows
 * lamplight gold; paused breathes moonlight, and the whole UI cools with it
 * (the data-power attribute swaps the accent tokens).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { Button, Sheet, cn } from "@/components/ui";

export function PowerOrb() {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);

  const power = useQuery({
    queryKey: ["power"],
    queryFn: api.power,
    refetchInterval: 15_000,
  });
  const paused = power.data?.state === "paused";

  // The whole shell follows the power state.
  useEffect(() => {
    document.documentElement.setAttribute("data-power", paused ? "paused" : "awake");
  }, [paused]);

  const toggle = useMutation({
    mutationFn: () => api.setPower(paused ? "idle" : "paused"),
    onSuccess: (status) => queryClient.setQueryData(["power"], status),
  });

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        aria-label={paused ? "epicurus is asleep — open power" : "epicurus is awake — open power"}
        className={cn(
          "relative flex size-9 items-center justify-center rounded-full border transition-colors",
          paused
            ? "border-accent/50 text-accent"
            : "border-accent/40 text-accent hover:border-accent",
        )}
      >
        <span
          className={cn(
            "absolute inset-0 rounded-full",
            paused ? "ep-breathe bg-accent-dim" : "bg-accent-dim",
          )}
        />
        {paused ? <Moon size={16} className="relative" /> : <Sun size={16} className="relative" />}
      </button>

      <Sheet open={open} onClose={() => setOpen(false)} title={paused ? "Asleep" : "Awake"}>
        <p className="text-sm leading-relaxed text-ink-dim">
          {paused ? (
            <>
              The garden rests. Local models are unloaded and the GPU is free for
              whatever else you are doing — chat will refuse local inference until
              you wake it (hosted fallbacks, if configured, still answer).
            </>
          ) : (
            <>
              Models load on demand and unload after a quiet spell on their own.
              Pausing unloads them <em>now</em> and keeps the machine free —
              epicurus waits without using it.
            </>
          )}
        </p>
        <div className="mt-4">
          <Button
            variant="primary"
            className="w-full"
            busy={toggle.isPending}
            onClick={() => toggle.mutate(undefined, { onSuccess: () => setOpen(false) })}
          >
            {paused ? "Wake up" : "Pause — unload models"}
          </Button>
        </div>
        {power.data && (
          <p className="mt-3 text-center text-xs text-ink-faint">
            state: {power.data.state}
          </p>
        )}
      </Sheet>
    </>
  );
}
