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

  it("never files a local model into recents (bare name or hf.co/ prefix)", () => {
    usePrefs.getState().setModel("hf.co/org/model:tag");
    expect(usePrefs.getState().recentModels).toEqual([]);
    usePrefs.getState().setModel("llama3.2");
    expect(usePrefs.getState().recentModels).toEqual([]);
  });
});
