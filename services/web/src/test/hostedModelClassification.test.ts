import { beforeEach, describe, expect, it } from "vitest";

import { isHostedModelId } from "@/lib/format";
import { usePrefs } from "@/stores/prefs";

// The two halves of the client-side fix for the old `includes("/")` heuristic that mis-filed a
// local `hf.co/org/model:tag` as a hosted model (#496).

describe("isHostedModelId", () => {
  it("recognises known hosted provider prefixes", () => {
    expect(isHostedModelId("claude/claude-3-5-sonnet-latest")).toBe(true);
    expect(isHostedModelId("gpt/gpt-4o")).toBe(true);
    expect(isHostedModelId("custom/my-model")).toBe(true);
  });

  it("classifies a two-slash aggregator id on its first segment (#865)", () => {
    // An OpenRouter model id carries a vendor segment of its own, so only the first slash
    // separates the provider alias — the id must not be split on every slash.
    expect(isHostedModelId("openrouter/anthropic/claude-sonnet-4.6")).toBe(true);
    expect(isHostedModelId("openrouter/openai/text-embedding-3-small")).toBe(true);
    // The vendor segment alone is not a provider alias, so it stays local.
    expect(isHostedModelId("anthropic/claude-sonnet-4.6")).toBe(false);
    expect(isHostedModelId("openai/text-embedding-3-small")).toBe(false);
  });

  it("treats bare names, the local alias, and unknown prefixes as local", () => {
    expect(isHostedModelId("llama3.2")).toBe(false);
    expect(isHostedModelId("qwen2.5:0.5b")).toBe(false);
    expect(isHostedModelId("local/llama3.2")).toBe(false);
    expect(isHostedModelId("hf.co/org/model:tag")).toBe(false); // the original bug
    expect(isHostedModelId("")).toBe(false);
    expect(isHostedModelId("/leading")).toBe(false);
  });
});

describe("usePrefs.setModel recents classification", () => {
  beforeEach(() => {
    usePrefs.setState({ model: null, recentModels: [] });
  });

  it("adds a genuine hosted id to the local recents cache", () => {
    usePrefs.getState().setModel("claude/sonnet");
    expect(usePrefs.getState().model).toBe("claude/sonnet");
    expect(usePrefs.getState().recentModels).toContain("claude/sonnet");
  });

  it("keeps a two-slash OpenRouter id intact in recents (#865)", () => {
    usePrefs.getState().setModel("openrouter/anthropic/claude-sonnet-4.6");
    expect(usePrefs.getState().recentModels).toContain("openrouter/anthropic/claude-sonnet-4.6");
  });

  it("never files a local model into recents (bare name or hf.co/ prefix)", () => {
    usePrefs.getState().setModel("hf.co/org/model:tag");
    expect(usePrefs.getState().recentModels).toEqual([]);
    usePrefs.getState().setModel("llama3.2");
    expect(usePrefs.getState().recentModels).toEqual([]);
  });
});
