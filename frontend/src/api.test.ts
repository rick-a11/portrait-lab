import { describe, expect, it, vi } from "vitest";

import {
  ApiError,
  animatePortrait,
  connectLocalApi,
  localApiOrigins,
  resolveApiUrl,
  restorePhoto,
  waitForJob,
  type Job,
  type RestoreResult,
} from "./api";

function job<TResult>(overrides: Partial<Job<TResult>> & Pick<Job<TResult>, "id">): Job<TResult> {
  return {
    kind: "restore",
    status: "queued",
    createdAt: "2026-10-26T00:00:00.000Z",
    startedAt: null,
    finishedAt: null,
    queuePosition: 0,
    error: null,
    result: null,
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

const noWait = () => Promise.resolve();

describe("resolveApiUrl", () => {
  it("joins an API origin and path without duplicate slashes", () => {
    expect(resolveApiUrl("/api/health", "http://127.0.0.1:5000/")).toBe(
      "http://127.0.0.1:5000/api/health",
    );
  });

  it("keeps a same-origin path relative", () => {
    expect(resolveApiUrl("/api/health", "")).toBe("/api/health");
  });

  it("uses the same local fallback port range as the service launcher", () => {
    expect(localApiOrigins()).toEqual([
      "http://127.0.0.1:5000",
      "http://127.0.0.1:5001",
      "http://127.0.0.1:5002",
      "http://127.0.0.1:5003",
    ]);
  });

  it("falls back to the next local API port when the default port is unavailable", async () => {
    const fetcher = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      if (String(input).startsWith("http://127.0.0.1:5000")) {
        return Promise.reject(new TypeError("Network error"));
      }
      return Promise.resolve(
        new Response(
          JSON.stringify({ status: "ok", model: { mode: "gfpgan", ready: true } }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    });

    const connection = await connectLocalApi({
      origins: ["http://127.0.0.1:5000", "http://127.0.0.1:5001"],
      fetcher,
    });

    expect(connection.origin).toBe("http://127.0.0.1:5001");
    expect(connection.health.model.mode).toBe("gfpgan");
  });

  it("abandons an unresponsive local port before trying the fallback", async () => {
    let defaultProbeWasCancelable = false;
    const fetcher = vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).startsWith("http://127.0.0.1:5000")) {
        const signal = init?.signal;
        defaultProbeWasCancelable = signal instanceof AbortSignal;
        if (!signal) return Promise.reject(new Error("Probe is missing an abort signal"));
        return new Promise<Response>((_resolve, reject) => {
          signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
        });
      }
      return Promise.resolve(
        new Response(
          JSON.stringify({ status: "ok", model: { mode: "gfpgan", ready: true } }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    });

    const connection = await connectLocalApi({
      origins: ["http://127.0.0.1:5000", "http://127.0.0.1:5001"],
      fetcher,
      timeoutMs: 1,
    });

    expect(defaultProbeWasCancelable).toBe(true);
    expect(connection.origin).toBe("http://127.0.0.1:5001");
  });

  it("submits the image and scale, then polls the job until the restoration completes", async () => {
    const file = new File(["pixel"], "portrait.png", { type: "image/png" });
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ job: job({ id: "job-1", queuePosition: 1 }) }, 202))
      .mockResolvedValueOnce(jsonResponse({ job: job({ id: "job-1", status: "running", queuePosition: null }) }))
      .mockResolvedValueOnce(
        jsonResponse({
          job: job<RestoreResult>({
            id: "job-1",
            status: "completed",
            queuePosition: null,
            result: { model: { mode: "preview", ready: true }, resultUrl: "/media/results/job-1.png" },
          }),
        }),
      );
    const updates: string[] = [];

    const result = await restorePhoto(file, 2, {
      origin: "https://api.example.test/",
      fetcher,
      sleep: noWait,
      onUpdate: (update) => updates.push(`${update.status}:${update.queuePosition}`),
    });

    expect(result.resultUrl).toBe("/media/results/job-1.png");
    expect(updates).toEqual(["queued:1", "running:null", "completed:null"]);
    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      "https://api.example.test/api/restore",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetcher).toHaveBeenNthCalledWith(2, "https://api.example.test/api/jobs/job-1");
    const options = fetcher.mock.calls[0][1] as RequestInit;
    const sent = options.body as FormData;
    expect(sent.get("image")).toBe(file);
    expect(sent.get("scale")).toBe("2");
  });

  it("sends the source image and chosen motion to the animation endpoint", async () => {
    const file = new File(["pixel"], "portrait.png", { type: "image/png" });
    const fetcher = vi.fn().mockResolvedValue(
      jsonResponse(
        {
          job: job({
            id: "animation-1",
            kind: "animate",
            status: "completed",
            result: {
              animation: { mode: "liveportrait", ready: true },
              resultUrl: "/media/animations/animation-1/result.mp4",
            },
          }),
        },
        202,
      ),
    );

    const result = await animatePortrait(file, { motionId: "d0.mp4" }, {
      origin: "https://api.example.test/",
      fetcher,
    });

    expect(result.resultUrl).toBe("/media/animations/animation-1/result.mp4");
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher).toHaveBeenCalledWith(
      "https://api.example.test/api/animate",
      expect.objectContaining({ method: "POST" }),
    );
    const options = fetcher.mock.calls[0][1] as RequestInit;
    const sent = options.body as FormData;
    expect(sent.get("image")).toBe(file);
    expect(sent.get("motionId")).toBe("d0.mp4");
    expect(sent.get("drivingVideo")).toBeNull();
  });

  it("sends an authorized custom driving video without a sample motion id", async () => {
    const image = new File(["portrait"], "portrait.png", { type: "image/png" });
    const drivingVideo = new File(["motion"], "my-motion.mp4", { type: "video/mp4" });
    const fetcher = vi.fn().mockResolvedValue(
      jsonResponse(
        {
          job: job({
            id: "animation-2",
            kind: "animate",
            status: "completed",
            result: {
              animation: { mode: "liveportrait", ready: true },
              resultUrl: "/media/animations/animation-2/result.mp4",
            },
          }),
        },
        202,
      ),
    );

    await animatePortrait(image, { drivingVideo }, { origin: "https://api.example.test", fetcher });

    const options = fetcher.mock.calls[0][1] as RequestInit;
    const sent = options.body as FormData;
    expect(sent.get("image")).toBe(image);
    expect(sent.get("motionId")).toBeNull();
    expect(sent.get("drivingVideo")).toBe(drivingVideo);
  });

  it("reports a rejected submission without polling", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue(jsonResponse({ error: { code: "queue_full", message: "任务队列已满" } }, 429));

    const attempt = restorePhoto(new File(["pixel"], "portrait.png"), 2, { origin: "", fetcher, sleep: noWait });

    await expect(attempt).rejects.toMatchObject({ status: 429, code: "queue_full" });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});

describe("waitForJob", () => {
  it("throws the job's error when it fails", async () => {
    const fetcher = vi.fn().mockResolvedValue(
      jsonResponse({
        job: job({
          id: "job-2",
          kind: "animate",
          status: "failed",
          error: { code: "animation_failed", message: "动态生成失败" },
        }),
      }),
    );

    const error = await waitForJob(job({ id: "job-2", status: "running" }), {
      origin: "",
      fetcher,
      sleep: noWait,
    }).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ message: "动态生成失败", code: "animation_failed" });
  });

  it("keeps polling through brief network failures because the job runs on the server", async () => {
    const fetcher = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("Network error"))
      .mockRejectedValueOnce(new TypeError("Network error"))
      .mockResolvedValueOnce(
        jsonResponse({ job: job({ id: "job-3", status: "completed", result: { resultUrl: "/media/results/job-3.png" } }) }),
      );

    const result = await waitForJob<{ resultUrl: string }>(job({ id: "job-3" }), {
      origin: "",
      fetcher,
      sleep: noWait,
    });

    expect(result.resultUrl).toBe("/media/results/job-3.png");
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it("gives up after too many consecutive network failures", async () => {
    const fetcher = vi.fn().mockRejectedValue(new TypeError("Network error"));

    const attempt = waitForJob(job({ id: "job-4" }), { origin: "", fetcher, sleep: noWait, maxNetworkErrors: 2 });

    await expect(attempt).rejects.toBeInstanceOf(TypeError);
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it("stops immediately when the server no longer knows the job", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue(jsonResponse({ error: { code: "job_not_found", message: "没有找到这个任务。" } }, 404));

    const attempt = waitForJob(job({ id: "job-5" }), { origin: "", fetcher, sleep: noWait });

    await expect(attempt).rejects.toMatchObject({ status: 404, code: "job_not_found" });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
