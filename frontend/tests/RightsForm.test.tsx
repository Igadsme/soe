import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { RightsForm } from "../app/components/RightsForm";

const evidence = "I own the original video and all distribution rights.";
function setup() {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ source_id: 42 }) });
  vi.stubGlobal("fetch", fetch);
  const onApproved = vi.fn();
  render(<RightsForm sourceId={42} onApproved={onApproved} />);
  return { user: userEvent.setup(), fetch, onApproved };
}
describe("RightsForm", () => {
  it.each(["", "too short", "a b c d e f g h i j k l m n o p q r s"])("blocks insufficient non-whitespace evidence: %s", async (value) => {
    const { user, fetch } = setup();
    if (value) await user.type(screen.getByLabelText("Evidence"), value);
    await user.click(screen.getByLabelText("youtube"));
    await user.click(screen.getByRole("button", { name: "Approve rights" }));
    expect(screen.getByRole("alert").textContent).toContain("20 non-whitespace");
    expect(fetch).not.toHaveBeenCalled();
  });
  it("requires an allowed platform", async () => {
    const { user, fetch } = setup();
    await user.type(screen.getByLabelText("Evidence"), evidence);
    await user.click(screen.getByRole("button", { name: "Approve rights" }));
    expect(screen.getByRole("alert").textContent).toContain("at least one");
    expect(fetch).not.toHaveBeenCalled();
  });
  it("submits schema values, permissions, UTC expiry and CSRF", async () => {
    const { user, fetch, onApproved } = setup();
    document.cookie = "soe_csrf=test%20token; path=/";
    await user.selectOptions(screen.getByLabelText("Authorization type"), "explicit_creator_authorization");
    await user.type(screen.getByLabelText("Evidence"), evidence);
    await user.click(screen.getByLabelText("youtube"));
    await user.click(screen.getByLabelText("instagram"));
    await user.click(screen.getByLabelText("Commercial use"));
    await user.click(screen.getByLabelText("Publishing allowed"));
    await user.type(screen.getByLabelText(/Expires at/), "2027-06-15T12:30");
    await user.click(screen.getByRole("button", { name: "Approve rights" }));
    const [url, options] = fetch.mock.calls[0];
    expect(url).toMatch(/\/api\/v1\/sources\/42\/rights$/);
    expect(options.method).toBe("POST");
    expect(options.headers.get("X-CSRF-Token")).toBe("test token");
    expect(options.headers.get("Content-Type")).toBe("application/json");
    expect(options.credentials).toBe("include");
    expect(JSON.parse(options.body)).toEqual({ authorization_type: "explicit_creator_authorization", evidence,
      allowed_platforms: ["youtube", "instagram"], commercial_use: true, publishing_allowed: true,
      expires_at: new Date("2027-06-15T12:30").toISOString() });
    expect(onApproved).toHaveBeenCalledWith(42);
  });
  it.each(["Rights rejected", [{ loc: ["body", "evidence"], msg: "Evidence rejected" }]])("shows server 422 details: %j", async (detail) => {
    const { user, fetch, onApproved } = setup();
    fetch.mockResolvedValue({ ok: false, status: 422, json: async () => ({ detail }) });
    await user.type(screen.getByLabelText("Evidence"), evidence);
    await user.click(screen.getByLabelText("twitch"));
    await user.click(screen.getByRole("button", { name: "Approve rights" }));
    expect(screen.getByRole("alert").textContent).toContain(typeof detail === "string" ? detail : "body.evidence: Evidence rejected");
    expect(onApproved).not.toHaveBeenCalled();
  });
  it("defaults permissions to false and expiry to null", async () => {
    const { user, fetch } = setup();
    await user.type(screen.getByLabelText("Evidence"), evidence);
    await user.click(screen.getByLabelText("tiktok"));
    await user.click(screen.getByRole("button", { name: "Approve rights" }));
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toMatchObject({ authorization_type: "owned", commercial_use: false, publishing_allowed: false, expires_at: null });
  });
});
