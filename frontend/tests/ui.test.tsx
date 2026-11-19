import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Badge, ConfirmButton, DataState } from "../app/components/ui";
import { ClipCard } from "../app/components/ClipCard";
import { request } from "../app/lib/api";

const resource = (data: unknown, extra = {}) => ({ data, error: "", loading: false, reload: async () => {}, ...extra });

describe("DataState", () => {
  it("shows loading, error, empty and data states", () => {
    const { rerender } = render(<DataState resource={resource(null, { loading: true })} empty="none">{() => "data"}</DataState>);
    expect(screen.getByRole("status").textContent).toBe("Loading…");
    rerender(<DataState resource={resource(null, { error: "boom" })} empty="none">{() => "data"}</DataState>);
    expect(screen.getByRole("alert").textContent).toBe("boom");
    rerender(<DataState resource={resource([])} empty="nothing here">{() => "data"}</DataState>);
    expect(screen.getByText("nothing here")).toBeTruthy();
    rerender(<DataState resource={resource([1])} empty="nothing here">{(rows) => `rows:${(rows as number[]).length}`}</DataState>);
    expect(screen.getByText("rows:1")).toBeTruthy();
  });
});

describe("ConfirmButton", () => {
  it("requires an explicit confirmation", async () => {
    const onConfirm = vi.fn();
    render(<ConfirmButton label="Emergency stop" confirm="Stop everything?" onConfirm={onConfirm} danger />);
    fireEvent.click(screen.getByText("Emergency stop"));
    expect(onConfirm).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("Cancel"));
    fireEvent.click(screen.getByText("Emergency stop"));
    fireEvent.click(screen.getByText("Confirm"));
    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1));
  });
});

it("badges failure states", () => {
  render(<><Badge value="dead_letter" /><Badge value="approved" /><Badge value={null} /></>);
  expect(screen.getByText("dead letter").className).toContain("bad");
  expect(screen.getByText("approved").className).toContain("good");
  expect(screen.getByText("unknown").className).toContain("neutral");
});

describe("ClipCard", () => {
  const clip = { id: 7, source_id: 1, status: "qc_failed", start_seconds: 1, end_seconds: 9.5, description: "d", hook: "Big moment", score: 81.2,
    evidence: { transcript: "hello world", qc_reasons: ["audio too quiet"] }, output_path: "/x.mp4" };
  it("previews rendered media inline and surfaces QC reasons", () => {
    render(<ClipCard clip={clip} run={vi.fn()} editable />);
    const video = screen.getByLabelText("Preview of clip 7") as HTMLVideoElement;
    expect(video.getAttribute("src")).toContain("/api/v1/clips/7/download?inline=true");
    expect(screen.getByText(/audio too quiet/)).toBeTruthy();
    expect(screen.getByText("hello world")).toBeTruthy();
  });
  it("submits a re-render with edited bounds", async () => {
    const run = vi.fn(async (action: () => Promise<unknown>) => { await action(); });
    const fetch = vi.fn().mockResolvedValue({ ok: true, status: 202, json: async () => ({ job_id: "j" }) });
    vi.stubGlobal("fetch", fetch);
    render(<ClipCard clip={clip} run={run} editable />);
    fireEvent.click(screen.getByText("Adjust timing"));
    fireEvent.change(screen.getByLabelText("End (s)"), { target: { value: "8" } });
    fireEvent.click(screen.getByText("Re-render"));
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(fetch.mock.calls[0][0]).toContain("/api/v1/clips/7/rerender");
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ start_seconds: 1, end_seconds: 8 });
  });
  it("hides preview for unrendered clips", () => {
    render(<ClipCard clip={{ ...clip, output_path: null }} run={vi.fn()} />);
    expect(screen.getByText("Not rendered yet")).toBeTruthy();
    expect(screen.queryByText("Approve")).toBeNull();
  });
});

describe("request", () => {
  it("returns parsed data and raises server detail", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ a: 1 }) })
      .mockResolvedValueOnce({ ok: false, status: 409, json: async () => ({ detail: "OAuth platform not configured" }) }));
    expect(await request("/x")).toEqual({ a: 1 });
    await expect(request("/x", "POST", { b: 1 })).rejects.toThrow("OAuth platform not configured");
  });
  it("redirects to login on 401", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, pathname: "/library", assign });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 401, json: async () => ({ detail: "authentication required" }) }));
    await expect(request("/api/v1/clips")).rejects.toThrow("authentication required");
    expect(assign).toHaveBeenCalledWith("/login?expired=1");
  });
});
