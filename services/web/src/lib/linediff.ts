/**
 * A minimal line-level diff for the suggestion review overlay (#KB-refactor).
 *
 * The operator reviews an agent's proposed edit and can approve only *part* of it. We
 * diff the current document against the proposal, group the changes into hunks the
 * operator toggles, and reconstruct the merged text from the accepted hunks. Self-contained
 * (no diff dependency) and exactly reversible — `split("\n")`/`join("\n")` round-trips, so
 * accepting every hunk reproduces the proposal and accepting none reproduces the current.
 */

export type LineTag = "same" | "add" | "del";

export interface DiffLine {
  tag: LineTag;
  text: string;
}

/** One reviewable change: a maximal run of add/del lines (its `id` is its order index). */
export interface Hunk {
  id: number;
  lines: DiffLine[];
}

function toLines(text: string): string[] {
  // Treat the empty document as zero lines (not [""]) so a create/delete is one clean hunk.
  return text === "" ? [] : text.split("\n");
}

/** An LCS line diff of *before* → *after* as a flat, ordered list of tagged lines. */
export function diffLines(before: string, after: string): DiffLine[] {
  const a = toLines(before);
  const b = toLines(after);
  const n = a.length;
  const m = b.length;
  // dp[i][j] = LCS length of a[i:] and b[j:].
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ tag: "same", text: a[i] });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ tag: "del", text: a[i] });
      i++;
    } else {
      out.push({ tag: "add", text: b[j] });
      j++;
    }
  }
  while (i < n) out.push({ tag: "del", text: a[i++] });
  while (j < m) out.push({ tag: "add", text: b[j++] });
  return out;
}

/** Group a diff into change hunks (runs of add/del); `same` lines are the gaps between. */
export function toHunks(diff: DiffLine[]): Hunk[] {
  const hunks: Hunk[] = [];
  let i = 0;
  let id = 0;
  while (i < diff.length) {
    if (diff[i].tag === "same") {
      i++;
      continue;
    }
    const lines: DiffLine[] = [];
    while (i < diff.length && diff[i].tag !== "same") lines.push(diff[i++]);
    hunks.push({ id: id++, lines });
  }
  return hunks;
}

/**
 * Reconstruct the merged document from a diff: for each change hunk, take its `add` lines
 * when accepted (the proposal) or its `del` lines when not (keep the original); `same` lines
 * always stay. Hunk ids match {@link toHunks} (both number change-runs in order).
 */
export function mergeHunks(diff: DiffLine[], accepted: ReadonlySet<number>): string {
  const out: string[] = [];
  let i = 0;
  let id = 0;
  while (i < diff.length) {
    if (diff[i].tag === "same") {
      out.push(diff[i++].text);
      continue;
    }
    const dels: string[] = [];
    const adds: string[] = [];
    while (i < diff.length && diff[i].tag !== "same") {
      if (diff[i].tag === "del") dels.push(diff[i].text);
      else adds.push(diff[i].text);
      i++;
    }
    out.push(...(accepted.has(id) ? adds : dels));
    id++;
  }
  return out.join("\n");
}
