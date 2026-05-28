import { describe, it, expect, vi, afterEach } from "vitest";
import { formatCurrency, formatSeconds, slaUrgency, cn } from "@/lib/utils";

describe("formatCurrency", () => {
  it("formats zero", () => {
    expect(formatCurrency(0)).toBe("$0.0000");
  });

  it("formats cents-scale amount", () => {
    expect(formatCurrency(0.0042)).toBe("$0.0042");
  });

  it("formats whole dollar", () => {
    expect(formatCurrency(1)).toBe("$1.0000");
  });

  it("formats larger amount", () => {
    expect(formatCurrency(12.3456)).toBe("$12.3456");
  });
});

describe("formatSeconds", () => {
  it("returns seconds when under 1 minute", () => {
    expect(formatSeconds(45)).toBe("45s");
  });

  it("returns minutes and seconds for 90s", () => {
    expect(formatSeconds(90)).toBe("1m 30s");
  });

  it("returns hours and minutes for >1 hour", () => {
    expect(formatSeconds(3661)).toBe("1h 1m");
  });

  it("handles exactly 1 hour", () => {
    expect(formatSeconds(3600)).toBe("1h 0m");
  });
});

describe("slaUrgency", () => {
  afterEach(() => vi.useRealTimers());

  it("returns expired for past deadline", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2024-01-02T00:00:00Z"));
    expect(slaUrgency("2024-01-01T00:00:00Z")).toBe("expired");
  });

  it("returns high when < 4 hours remaining", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2024-01-01T22:00:00Z"));
    expect(slaUrgency("2024-01-02T00:00:00Z")).toBe("high"); // 2h remaining
  });

  it("returns medium when 4-12 hours remaining", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2024-01-01T16:00:00Z"));
    expect(slaUrgency("2024-01-02T00:00:00Z")).toBe("medium"); // 8h remaining
  });

  it("returns low when > 12 hours remaining", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2024-01-01T00:00:00Z"));
    expect(slaUrgency("2024-01-02T00:00:00Z")).toBe("low"); // 24h remaining
  });
});

describe("cn", () => {
  it("merges class names", () => {
    expect(cn("a", "b")).toBe("a b");
  });

  it("deduplicates conflicting tailwind classes", () => {
    expect(cn("text-red-500", "text-blue-500")).toBe("text-blue-500");
  });

  it("ignores falsy values", () => {
    expect(cn("a", false, undefined, null, "b")).toBe("a b");
  });
});
