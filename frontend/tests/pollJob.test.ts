import { expect, it, vi } from "vitest";
import { pollJob } from "../app/lib/pollJob";

it.each(["succeeded", "failed"])("stops polling on %s", async (status) => {
  vi.useFakeTimers();
  const terminal = { status, last_error: "Worker failed" };
  const fetch = vi.fn().mockResolvedValueOnce({ ok: true, json: async () => ({ status: "running" }) })
    .mockResolvedValue({ ok: true, json: async () => terminal });
  vi.stubGlobal("fetch", fetch);
  const onStatus = vi.fn();
  const promise = pollJob(123, { onStatus });
  await vi.advanceTimersByTimeAsync(2000);
  await expect(promise).resolves.toEqual(terminal);
  await vi.advanceTimersByTimeAsync(600000);
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(fetch.mock.calls[0][0]).toMatch(/\/api\/v1\/jobs\/123$/);
  expect(onStatus.mock.calls.map(([job]) => job.status)).toEqual(["running", status]);
  expect(vi.getTimerCount()).toBe(0);
});
it.each([false, true])("times out and stops, including a hung request (%s)", async (hung) => {
  vi.useFakeTimers();
  const fetch = vi.fn().mockImplementation(() => hung ? new Promise(() => {}) : Promise.resolve({ ok: true, json: async () => ({ status: "queued" }) }));
  vi.stubGlobal("fetch", fetch);
  const result = expect(pollJob(123, { timeoutMs: 5000 })).rejects.toThrow("timed out");
  await vi.advanceTimersByTimeAsync(5000);
  await result;
  const calls = fetch.mock.calls.length;
  await vi.advanceTimersByTimeAsync(600000);
  expect(fetch).toHaveBeenCalledTimes(calls);
  expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
  expect(vi.getTimerCount()).toBe(0);
});
