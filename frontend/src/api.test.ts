import { describe, expect, it, vi } from "vitest";

import { animatePortrait, connectLocalApi, localApiOrigins, resolveApiUrl, restorePhoto } from "./api";

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

  it("sends the selected image and scale to the restoration endpoint", async () => {
    const file = new File(["pixel"], "portrait.png", { type: "image/png" });
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          job: { id: "job-1", status: "completed" },
          model: { mode: "preview", ready: true },
          resultUrl: "/media/results/job-1.png",
        }),
        { status: 201, headers: { "content-type": "application/json" } },
      ),
    );

    const result = await restorePhoto(file, 2, {
      origin: "https://api.example.test/",
      fetcher,
    });

    expect(result.job.id).toBe("job-1");
    expect(fetcher).toHaveBeenCalledWith(
      "https://api.example.test/api/restore",
      expect.objectContaining({ method: "POST" }),
    );
    const options = fetcher.mock.calls[0][1] as RequestInit;
    const sent = options.body as FormData;
    expect(sent.get("image")).toBe(file);
    expect(sent.get("scale")).toBe("2");
  });

  it("sends the source image and chosen motion to the animation endpoint", async () => {
    const file = new File(["pixel"], "portrait.png", { type: "image/png" });
    const fetcher = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          job: { id: "animation-1", status: "completed" },
          animation: { mode: "liveportrait", ready: true },
          resultUrl: "/media/animations/animation-1/result.mp4",
        }),
        { status: 201, headers: { "content-type": "application/json" } },
      ),
    );

    const result = await animatePortrait(file, { motionId: "d0.mp4" }, {
      origin: "https://api.example.test/",
      fetcher,
    });

    expect(result.job.id).toBe("animation-1");
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
      new Response(
        JSON.stringify({
          job: { id: "animation-2", status: "completed" },
          animation: { mode: "liveportrait", ready: true },
          resultUrl: "/media/animations/animation-2/result.mp4",
        }),
        { status: 201, headers: { "content-type": "application/json" } },
      ),
    );

    await animatePortrait(image, { drivingVideo }, { origin: "https://api.example.test", fetcher });

    const options = fetcher.mock.calls[0][1] as RequestInit;
    const sent = options.body as FormData;
    expect(sent.get("image")).toBe(image);
    expect(sent.get("motionId")).toBeNull();
    expect(sent.get("drivingVideo")).toBe(drivingVideo);
  });
});
