import { describe, expect, it } from "vitest";

import { formatBytes, recencyBucket } from "@/lib/format";

// A fixed "now" mid-afternoon, away from midnight, so day arithmetic is unambiguous.
const NOW = new Date(2026, 6, 3, 15, 0, 0); // Jul 3 2026, 15:00 local

describe("recencyBucket", () => {
  it("is Today for any moment of the same calendar day", () => {
    expect(recencyBucket(new Date(2026, 6, 3, 0, 5), NOW)).toBe("Today");
    expect(recencyBucket(new Date(2026, 6, 3, 14, 59), NOW)).toBe("Today");
  });

  it("is Yesterday across the calendar boundary, not a rolling 24h", () => {
    // 23:50 the night before is 15h10m ago — but a different calendar day.
    expect(recencyBucket(new Date(2026, 6, 2, 23, 50), NOW)).toBe("Yesterday");
    expect(recencyBucket(new Date(2026, 6, 2, 0, 1), NOW)).toBe("Yesterday");
  });

  it("rolls This week / This month as 7- and 30-day windows", () => {
    expect(recencyBucket(new Date(2026, 6, 1, 8, 0), NOW)).toBe("This week"); // 2 days
    expect(recencyBucket(new Date(2026, 5, 27, 8, 0), NOW)).toBe("This week"); // 6 days
    expect(recencyBucket(new Date(2026, 5, 26, 8, 0), NOW)).toBe("This month"); // 7 days
    expect(recencyBucket(new Date(2026, 5, 4, 8, 0), NOW)).toBe("This month"); // 29 days
  });

  it("is Earlier from 30 days out", () => {
    expect(recencyBucket(new Date(2026, 5, 3, 8, 0), NOW)).toBe("Earlier"); // 30 days
    expect(recencyBucket(new Date(2025, 0, 1), NOW)).toBe("Earlier");
  });

  it("treats a future timestamp (clock skew) as Today rather than crashing", () => {
    expect(recencyBucket(new Date(2026, 6, 4, 9, 0), NOW)).toBe("Today");
  });
});

describe("formatBytes", () => {
  it("keeps existing behaviour", () => {
    expect(formatBytes(9_300_000_000)).toBe("8.7 GB");
    expect(formatBytes(null)).toBe("");
  });
});
