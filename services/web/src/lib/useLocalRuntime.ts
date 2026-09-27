/**
 * Does this deployment have a local model runtime? (#962)
 *
 * One query, one answer, read by every surface that used to assume Ollama existed. It exists
 * because the shell had no way to tell three different things apart — a runtime that is *absent
 * by design* (`OLLAMA_URL=""`: hosted chat, hosted embeddings, no Ollama), one that is
 * configured and **unreachable**, and one that is fine — so it rendered the pull card, the
 * catalog, the KV-cache card and a `Local (Ollama)` optgroup on a deployment where none of them
 * could ever do anything, and called the whole situation "is the ollama service up?".
 *
 * Two rules this hook exists to keep:
 *
 * 1. **The state comes from `/llm/local-runtime`, never from a failing `/llm/models`.** Since
 *    #962 the model list answers `[]` with a 200 when the runtime is absent *or* unreachable,
 *    so an error there carries no information at all. Any code that goes back to inferring the
 *    state from a query failure silently stops working.
 * 2. **An older core, or an unreachable one, reads as `ok`.** A core that predates the endpoint
 *    404s; treating that as "absent" would hide the local half of the Models page from a
 *    deployment that has a perfectly good runtime. `ok` is exactly what every surface assumed
 *    before this change, so an unknown answer keeps today's behaviour.
 */
import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import type { LocalRuntimeState } from "@/lib/contracts";

export interface LocalRuntime {
  /** `absent` | `unreachable` | `ok` — `ok` until the endpoint says otherwise. */
  state: LocalRuntimeState;
  /** No local runtime on this deployment, deliberately. The one flag most callers want. */
  absent: boolean;
  /** A runtime is configured and cannot be reached. An error, and it still looks like one. */
  unreachable: boolean;
  /** The endpoint has answered (or failed) at least once. Gate a *collapse* on this so a
   *  hosted-only deployment never flashes the local half of the page before hiding it again. */
  settled: boolean;
}

export function useLocalRuntime(): LocalRuntime {
  const status = useQuery({
    queryKey: ["localRuntime"],
    queryFn: () => api.localRuntime(),
    // A deployment does not grow or lose a runtime between renders; one fetch per session is
    // plenty, and a stale answer here is cheaper than polling a constant.
    staleTime: 5 * 60_000,
    // A 404 from an older core is a final answer, not a blip worth three attempts.
    retry: false,
  });
  const state: LocalRuntimeState = status.data?.state ?? "ok";
  return {
    state,
    absent: state === "absent",
    unreachable: state === "unreachable",
    // `isFetched` and not `!isPending`, because this gates whether the local half of the Models
    // page is mounted at all, and those components subscribe to this very query. An errored
    // query (an older core's 404) is permanently stale, so each newly-mounted subscriber
    // triggers a refetch — which, with no data to fall back on, puts the query back into
    // `pending` and unmounts them again: a page that oscillates forever. `isFetched` latches
    // true after the first attempt, so the answer can change but the mounting decision can't
    // feed back into it.
    settled: status.isFetched,
  };
}
