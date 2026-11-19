import { describe, expect, it } from "vitest";
import { publishPayload, validatePublish } from "../app/lib/publishForm";
import { dollarsToCents, formatWindows, parseWindows } from "../app/lib/policy";
import { totalActive, totalsByMetric } from "../app/lib/metrics";

const base = { platform: "youtube", account_id: 1, title: "t", description: "", privacy_status: "private", scheduled_at: "" };

describe("publish form", () => {
  it("accepts a valid YouTube request", () => expect(validatePublish(base)).toEqual([]));
  it("enforces API length limits", () => {
    expect(validatePublish({ ...base, title: "x".repeat(101), description: "y".repeat(5001) })).toEqual([
      "Title must be 100 characters or fewer.", "Description must be 5000 characters or fewer."]);
  });
  it("rejects unlisted outside YouTube and requires Instagram acknowledgement", () => {
    expect(validatePublish({ ...base, platform: "tiktok", privacy_status: "unlisted" })).toContain("Unlisted visibility is available for YouTube only.");
    expect(validatePublish({ ...base, platform: "instagram" })).toContain("Acknowledge that Instagram Reels use the account’s visibility.");
    expect(validatePublish({ ...base, platform: "instagram", instagram_visibility_acknowledged: true })).toEqual([]);
  });
  it("requires an account and a future schedule", () => {
    const now = new Date("2026-11-19T12:00:00Z");
    expect(validatePublish({ ...base, account_id: 0, scheduled_at: "2026-11-18T12:00" }, now)).toEqual(["Choose a connected account.", "Scheduled time must be in the future."]);
  });
  it("builds platform-specific payloads without empty fields", () => {
    expect(publishPayload({ ...base, title: "", platform: "tiktok" })).toMatchObject({ title: undefined, tiktok_privacy_level: "SELF_ONLY", instagram_visibility_acknowledged: undefined });
    expect(publishPayload({ ...base, scheduled_at: "2030-01-01T10:00" }).scheduled_at).toMatch(/^2030-01-01T/);
  });
});

describe("policy helpers", () => {
  it("round-trips publishing windows", () => {
    const windows = parseWindows("9-12, 18 - 24");
    expect(windows).toEqual([{ start_hour: 9, end_hour: 12 }, { start_hour: 18, end_hour: 24 }]);
    expect(formatWindows(windows)).toBe("9-12, 18-24");
    expect(parseWindows("")).toEqual([]);
  });
  it.each(["9", "a-b", "25-3", "5-5"])("rejects malformed window %s", (text) => expect(() => parseWindows(text)).toThrow());
  it("converts budgets and keeps blank as no ceiling", () => {
    expect(dollarsToCents("")).toBeNull();
    expect(dollarsToCents("12.345")).toBe(1235);
    expect(() => dollarsToCents("-1")).toThrow();
  });
});

describe("metrics", () => {
  it("sums by metric", () => expect(totalsByMetric([{ metric: "views", value: 2, publication_id: 1 }, { metric: "views", value: 3, publication_id: 2 }, { metric: "likes", value: 1, publication_id: 1 }]))
    .toEqual([{ metric: "views", value: 5 }, { metric: "likes", value: 1 }]));
  it("counts only active jobs", () => expect(totalActive({ emergency_stopped: false, worker_heartbeat_age_seconds: null,
    queue_depth: { queued: { publish: 2 }, running: { process_source: 1 }, succeeded: { publish: 9 } } })).toBe(3));
});
