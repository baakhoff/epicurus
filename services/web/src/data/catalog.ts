/** Built-in fallback catalog + the model browser's filter helpers.
 *
 * The live model list now comes from the core (`GET /platform/v1/llm/catalog`), which
 * parses an upstream library on a schedule (#269). This `CATALOG` is the offline seed the
 * Models screen falls back to when that endpoint is unreachable (e.g. an older core), so
 * the browser is never empty. `CatalogTag` / `filterCatalog` / `formatGb` are the seam the
 * core's entries flow through unchanged.
 */
import type { CatalogEntry } from "@/lib/contracts";

export type { CatalogEntry };

export const ALL_TAGS = ["general", "code", "multilingual", "vision", "embedding", "small"] as const;
export type CatalogTag = (typeof ALL_TAGS)[number];

export const TAG_LABELS: Record<CatalogTag, string> = {
  general: "General",
  code: "Code",
  multilingual: "Multilingual",
  vision: "Vision",
  embedding: "Embedding",
  small: "Small (<2 GB)",
};

export const CATALOG: CatalogEntry[] = [
  // ── Small / general ──────────────────────────────────────────────────────
  {
    id: "smollm2:135m",
    family: "SmolLM2",
    params: "135M",
    size_gb: 0.09,
    description: "Tiny but capable general-purpose chat model — fits in a browser tab's RAM.",
    tags: ["general", "small"],
  },
  {
    id: "smollm2:360m",
    family: "SmolLM2",
    params: "360M",
    size_gb: 0.22,
    description: "A step up from 135M with better reasoning on device.",
    tags: ["general", "small"],
  },
  {
    id: "smollm2:1.7b",
    family: "SmolLM2",
    params: "1.7B",
    size_gb: 1.1,
    description: "Punches above its weight for summarisation and short Q&A.",
    tags: ["general", "small"],
  },
  {
    id: "gemma3:1b",
    family: "Gemma 3",
    params: "1B",
    size_gb: 0.82,
    description: "Google's smallest Gemma release — fast and surprisingly fluent.",
    tags: ["general", "small"],
  },
  {
    id: "phi4-mini:3.8b",
    family: "Phi-4 Mini",
    params: "3.8B",
    size_gb: 2.5,
    description: "Microsoft's efficiency-first model; strong at instruction following.",
    tags: ["general", "small"],
  },
  {
    id: "llama3.2:1b",
    family: "Llama 3.2",
    params: "1B",
    size_gb: 1.3,
    description: "Meta's smallest Llama — quick to spin up, good for embedded use.",
    tags: ["general", "small"],
  },
  {
    id: "llama3.2:3b",
    family: "Llama 3.2",
    params: "3B",
    size_gb: 2.0,
    description: "Solid all-rounder that fits on any modern laptop.",
    tags: ["general", "small"],
  },

  // ── General (mid / large) ────────────────────────────────────────────────
  {
    id: "mistral:7b",
    family: "Mistral",
    params: "7B",
    size_gb: 4.1,
    description: "Mistral AI's flagship 7B — fast, concise, and good at reasoning.",
    tags: ["general"],
  },
  {
    id: "gemma3:4b",
    family: "Gemma 3",
    params: "4B",
    size_gb: 2.6,
    description: "Balanced Google model with strong multilingual support.",
    tags: ["general", "multilingual"],
  },
  {
    id: "gemma3:12b",
    family: "Gemma 3",
    params: "12B",
    size_gb: 8.1,
    description: "Google's best local Gemma; excels at long-context tasks.",
    tags: ["general", "multilingual"],
  },
  {
    id: "llama3.1:8b",
    family: "Llama 3.1",
    params: "8B",
    size_gb: 4.9,
    description: "The 128 K context window model from Meta — great for long documents.",
    tags: ["general"],
  },
  {
    id: "phi4:14b",
    family: "Phi-4",
    params: "14B",
    size_gb: 9.1,
    description: "Microsoft's best local Phi — competitive with much larger models.",
    tags: ["general"],
  },
  {
    id: "llama3.3:70b",
    family: "Llama 3.3",
    params: "70B",
    size_gb: 43.0,
    description: "Near-frontier quality if you have the VRAM (or patience).",
    tags: ["general"],
  },

  // ── Code ─────────────────────────────────────────────────────────────────
  {
    id: "qwen2.5-coder:1.5b",
    family: "Qwen 2.5 Coder",
    params: "1.5B",
    size_gb: 1.0,
    description: "Tiny but surprisingly capable code assistant.",
    tags: ["code", "small"],
  },
  {
    id: "qwen2.5-coder:7b",
    family: "Qwen 2.5 Coder",
    params: "7B",
    size_gb: 4.7,
    description: "Alibaba's best open code model at 7B — strong completions and debugging.",
    tags: ["code"],
  },
  {
    id: "codellama:7b",
    family: "Code Llama",
    params: "7B",
    size_gb: 3.8,
    description: "Meta's fine-tune for code: completion, infill, and chat modes.",
    tags: ["code"],
  },
  {
    id: "deepseek-coder-v2:16b",
    family: "DeepSeek Coder V2",
    params: "16B",
    size_gb: 8.9,
    description: "Top-tier code model — MoE architecture for quality at lower resource cost.",
    tags: ["code"],
  },

  // ── Multilingual ─────────────────────────────────────────────────────────
  {
    id: "qwen2.5:3b",
    family: "Qwen 2.5",
    params: "3B",
    size_gb: 2.0,
    description: "Alibaba's multilingual model, strong across 29 languages.",
    tags: ["multilingual", "general", "small"],
  },
  {
    id: "qwen2.5:7b",
    family: "Qwen 2.5",
    params: "7B",
    size_gb: 4.7,
    description: "Excellent multilingual reasoning and instruction following.",
    tags: ["multilingual", "general"],
  },

  // ── Embedding ────────────────────────────────────────────────────────────
  {
    id: "nomic-embed-text",
    family: "Nomic Embed",
    params: "137M",
    size_gb: 0.27,
    description: "Fast, high-quality text embeddings — the go-to for local RAG.",
    tags: ["embedding", "small"],
  },
  {
    id: "mxbai-embed-large",
    family: "mxbai-embed",
    params: "334M",
    size_gb: 0.67,
    description: "Best-in-class retrieval quality for semantic search.",
    tags: ["embedding", "small"],
  },

  // ── Vision ───────────────────────────────────────────────────────────────
  {
    id: "moondream:1.8b",
    family: "Moondream",
    params: "1.8B",
    size_gb: 1.1,
    description: "Efficient vision-language model for image Q&A on any hardware.",
    tags: ["vision", "small"],
  },
  {
    id: "llava:7b",
    family: "LLaVA",
    params: "7B",
    size_gb: 4.7,
    description: "Original open multimodal model — describe and analyse images.",
    tags: ["vision"],
  },
  {
    id: "llama3.2-vision:11b",
    family: "Llama 3.2 Vision",
    params: "11B",
    size_gb: 7.9,
    description: "Meta's frontier-quality multimodal Llama with vision support.",
    tags: ["vision"],
  },
];

export function filterCatalog(
  entries: CatalogEntry[],
  query: string,
  tag: CatalogTag | null,
): CatalogEntry[] {
  const q = query.toLowerCase().trim();
  return entries.filter((e) => {
    if (tag !== null && !e.tags.includes(tag)) return false;
    if (!q) return true;
    return (
      e.id.toLowerCase().includes(q) ||
      e.family.toLowerCase().includes(q) ||
      e.description.toLowerCase().includes(q)
    );
  });
}

export function formatGb(gb: number): string {
  if (gb < 1) return `${Math.round(gb * 1000)} MB`;
  return `${gb >= 10 ? Math.round(gb) : gb.toFixed(1)} GB`;
}
