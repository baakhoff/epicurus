/**
 * A tiny subsequence scorer for the command palette (#491) — deliberately not a
 * dependency. Matching: every needle character must appear in the haystack in order,
 * case-insensitively; spaces in the needle are fragment separators and never have to
 * match. Scoring is greedy left-to-right — not optimal alignment, but deterministic,
 * O(n·m) worst case, and good enough for a few dozen palette entries.
 *
 * The weights encode what a fragment-typing human means:
 *   +2 per matched character           (longer matches count for more)
 *   +4 when the match starts a word    ("ca" should light up "Calendar", not "Local")
 *   +3 when it extends the previous match (consecutive runs beat scattered letters)
 *   −1 per leading character skipped, capped at −6 (early hits beat buried ones)
 */

const BOUNDARY = new Set([" ", "-", "_", "/", "."]);

/** Score `needle` against `haystack`; null = not a subsequence (filter the entry out). */
export function fuzzyScore(needle: string, haystack: string): number | null {
  const n = needle.trim().toLowerCase();
  if (!n) return 0; // an empty query matches everything, indifferently
  const h = haystack.toLowerCase();
  let score = 0;
  let from = 0;
  let prev = -2; // never adjacent to the first match
  let first = -1;
  for (const ch of n) {
    if (ch === " ") continue;
    const at = h.indexOf(ch, from);
    if (at < 0) return null;
    score += 2;
    if (at === 0 || BOUNDARY.has(h[at - 1])) score += 4;
    if (at === prev + 1) score += 3;
    if (first < 0) first = at;
    prev = at;
    from = at + 1;
  }
  return score - Math.min(first, 6);
}

/**
 * Filter + rank `items` by how well `label(item)` matches the query. Non-matches drop
 * out; ties keep the incoming order (so recency/nav order is the tiebreak, stable).
 */
export function rankFiltered<T>(items: T[], needle: string, label: (item: T) => string): T[] {
  return items
    .map((item, index) => ({ item, index, score: fuzzyScore(needle, label(item)) }))
    .filter((r): r is { item: T; index: number; score: number } => r.score != null)
    .sort((a, b) => b.score - a.score || a.index - b.index)
    .map((r) => r.item);
}
