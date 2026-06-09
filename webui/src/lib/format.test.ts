import { describe, expect, it } from "vitest";
import { fmtTokens, fmtUsd, shortTime } from "./format";

describe("format helpers", () => {
  it("formats usd with the same tiers as the backend", () => {
    expect(fmtUsd(0)).toBe("$0.00");
    expect(fmtUsd(-1)).toBe("$0.00");
    expect(fmtUsd(0.004)).toBe("$0.004");
    expect(fmtUsd(0.5)).toBe("$0.50");
    expect(fmtUsd(12.345)).toBe("$12.35");
  });

  it("abbreviates token counts", () => {
    expect(fmtTokens(42)).toBe("42");
    expect(fmtTokens(1500)).toBe("1.5k");
    expect(fmtTokens(2_000_000)).toBe("2.0M");
  });

  it("extracts HH:MM:SS from a naive ISO timestamp", () => {
    expect(shortTime("2026-06-09T13:45:07.123")).toBe("13:45:07");
    expect(shortTime("")).toBe("");
  });
});
