import { describe, expect, it } from "vitest";

import { normalizeRule, presetForRule, repeatLabel, REPEAT_PRESETS } from "@/lib/rrule";

describe("rrule presets (#471)", () => {
  it("maps empty and named rules to their preset", () => {
    expect(presetForRule("")).toBe("none");
    expect(presetForRule("FREQ=DAILY")).toBe("daily");
    expect(presetForRule("FREQ=WEEKLY")).toBe("weekly");
    expect(presetForRule("FREQ=MONTHLY")).toBe("monthly");
    expect(presetForRule("FREQ=YEARLY")).toBe("yearly");
    expect(presetForRule("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR")).toBe("weekdays");
  });

  it("recognizes a preset regardless of part order or case", () => {
    expect(presetForRule("BYDAY=mo,tu,we,th,fr;FREQ=weekly")).toBe("weekdays");
  });

  it("strips a leading RRULE: prefix", () => {
    expect(presetForRule("RRULE:FREQ=DAILY")).toBe("daily");
  });

  it("falls back to custom for anything the presets don't cover", () => {
    expect(presetForRule("FREQ=WEEKLY;INTERVAL=2")).toBe("custom");
    expect(presetForRule("FREQ=MONTHLY;BYMONTHDAY=1")).toBe("custom");
  });

  it("normalizes a rule to uppercase, sorted parts", () => {
    expect(normalizeRule("freq=weekly;byday=mo")).toBe("BYDAY=MO;FREQ=WEEKLY");
    expect(normalizeRule("")).toBe("");
    expect(normalizeRule("RRULE:FREQ=DAILY")).toBe("FREQ=DAILY");
  });

  it("gives a short human label", () => {
    expect(repeatLabel("")).toBe("");
    expect(repeatLabel("FREQ=WEEKLY")).toBe("Weekly");
    expect(repeatLabel("FREQ=WEEKLY;INTERVAL=3")).toBe("Custom");
  });

  it("offers none first and custom last", () => {
    expect(REPEAT_PRESETS[0].id).toBe("none");
    expect(REPEAT_PRESETS[REPEAT_PRESETS.length - 1].id).toBe("custom");
  });
});
