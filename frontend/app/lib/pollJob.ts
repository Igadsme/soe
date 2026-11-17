import { api, apiFetch } from "./api";

export type Job = { status: string; last_error?: string; result?: { clips_generated: number; transcript_segments: number } };

export async function pollJob(jobId: string | number, { onStatus, intervalMs = 2000, timeoutMs = 300000 }: {
  onStatus?: (job: Job) => void; intervalMs?: number; timeoutMs?: number;
} = {}): Promise<Job> {
  const controller = new AbortController();
  let deadline: ReturnType<typeof setTimeout>;
  let delay: ReturnType<typeof setTimeout>;
  const timeout = new Promise<never>((_, reject) => {
    deadline = setTimeout(() => {
      reject(new Error("Processing status timed out. Check again later."));
      controller.abort();
    }, timeoutMs);
  });
  try {
    for (;;) {
      const job: Job = await Promise.race([
        apiFetch(`${api}/api/v1/jobs/${jobId}`, { signal: controller.signal }).then((response) => response.json()), timeout,
      ]);
      onStatus?.(job);
      if (job.status === "succeeded" || job.status === "failed") return job;
      await Promise.race([new Promise<void>((resolve) => { delay = setTimeout(resolve, intervalMs); }), timeout]);
    }
  } finally { clearTimeout(deadline!); clearTimeout(delay!); }
}
