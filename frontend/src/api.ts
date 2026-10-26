export function resolveApiUrl(path: string, origin: string): string {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const normalizedOrigin = origin.replace(/\/+$/, "");
  return normalizedOrigin ? `${normalizedOrigin}${normalizedPath}` : normalizedPath;
}

export type ModelStatus = {
  mode: "preview" | "gfpgan" | string;
  ready: boolean;
  message?: string;
};

export type HealthResponse = {
  status: "ok";
  model: ModelStatus;
  animation?: ModelStatus;
  jobs?: { maxPending: number; kinds: Record<string, JobLoad> };
};

export type JobStatus = "queued" | "running" | "completed" | "failed";

export type Job<TResult> = {
  id: string;
  kind: "restore" | "animate";
  status: JobStatus;
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  /** Jobs of the same kind queued ahead of this one; null once it has started. */
  queuePosition: number | null;
  error: { code: string; message: string } | null;
  result: TResult | null;
};

export type JobResponse<TResult> = { job: Job<TResult> };

export type RestoreResult = {
  model: ModelStatus;
  message?: string;
  sourceUrl?: string;
  resultUrl: string;
};

export type AnimateResult = {
  animation: ModelStatus;
  message?: string;
  resultUrl: string;
};

export type JobLoad = { concurrency: number; queued: number; running: number };

export type MotionResponse = {
  scope: "local";
  items: Array<{
    id: string;
    label: string;
    description: string;
    duration: string;
    previewUrl: string;
  }>;
};

export type AnimationDriver = { motionId: string } | { drivingVideo: File };

export type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

export type JobWatchOptions<TResult> = {
  origin?: string;
  fetcher?: Fetcher;
  intervalMs?: number;
  /** Consecutive network failures tolerated while polling; the job keeps running on the server. */
  maxNetworkErrors?: number;
  onUpdate?: (job: Job<TResult>) => void;
  sleep?: (ms: number) => Promise<void>;
};

const LOCAL_API_PROBE_TIMEOUT_MS = 1_500;
const DEFAULT_POLL_INTERVAL_MS = 1_000;
const DEFAULT_MAX_NETWORK_ERRORS = 5;

const wait = (ms: number) => new Promise<void>((resolve) => globalThis.setTimeout(resolve, ms));

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly code?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export function defaultApiOrigin(): string {
  return localApiOrigins()[0];
}

export function localApiOrigins(): string[] {
  const configured = import.meta.env.VITE_API_BASE_URL?.trim();
  if (configured) return [configured];
  return [
    "http://127.0.0.1:5000",
    "http://127.0.0.1:5001",
    "http://127.0.0.1:5002",
    "http://127.0.0.1:5003",
  ];
}

async function decode<T>(response: Response): Promise<T> {
  const payload = (await response.json().catch(() => null)) as
    | T
    | { error?: { message?: string; code?: string } }
    | null;
  if (!response.ok) {
    const error = payload as { error?: { message?: string; code?: string } } | null;
    throw new ApiError(
      error?.error?.message || "服务暂时无法处理请求。",
      response.status,
      error?.error?.code,
    );
  }
  return payload as T;
}

export async function getHealth(
  options: { origin?: string; fetcher?: Fetcher } = {},
): Promise<HealthResponse> {
  const origin = options.origin ?? defaultApiOrigin();
  const fetcher = options.fetcher ?? fetch;
  return decode<HealthResponse>(await fetcher(resolveApiUrl("/api/health", origin)));
}

export async function connectLocalApi(
  options: { origins?: string[]; fetcher?: Fetcher; timeoutMs?: number } = {},
): Promise<{ origin: string; health: HealthResponse }> {
  const origins = options.origins ?? localApiOrigins();
  const fetcher = options.fetcher ?? fetch;
  const timeoutMs = Math.max(1, options.timeoutMs ?? LOCAL_API_PROBE_TIMEOUT_MS);
  let lastError: unknown;

  for (const origin of origins) {
    try {
      const controller = new AbortController();
      const timeout = globalThis.setTimeout(() => controller.abort(), timeoutMs);
      let health: HealthResponse;
      try {
        health = await decode<HealthResponse>(
          await fetcher(resolveApiUrl("/api/health", origin), { signal: controller.signal }),
        );
      } finally {
        globalThis.clearTimeout(timeout);
      }
      return { origin, health };
    } catch (error) {
      lastError = error;
    }
  }

  throw lastError instanceof Error ? lastError : new Error("无法连接本地模型服务。");
}

export async function getMotions(
  options: { origin?: string; fetcher?: Fetcher } = {},
): Promise<MotionResponse> {
  const origin = options.origin ?? defaultApiOrigin();
  const fetcher = options.fetcher ?? fetch;
  return decode<MotionResponse>(await fetcher(resolveApiUrl("/api/motions", origin)));
}

export async function getJob<TResult>(
  jobId: string,
  options: { origin?: string; fetcher?: Fetcher } = {},
): Promise<Job<TResult>> {
  const origin = options.origin ?? defaultApiOrigin();
  const fetcher = options.fetcher ?? fetch;
  const response = await decode<JobResponse<TResult>>(
    await fetcher(resolveApiUrl(`/api/jobs/${encodeURIComponent(jobId)}`, origin)),
  );
  return response.job;
}

/** Poll a job until it finishes; resolves with its result or throws its error as an ApiError. */
export async function waitForJob<TResult>(
  job: Job<TResult>,
  options: JobWatchOptions<TResult> = {},
): Promise<TResult> {
  const intervalMs = options.intervalMs ?? DEFAULT_POLL_INTERVAL_MS;
  const maxNetworkErrors = options.maxNetworkErrors ?? DEFAULT_MAX_NETWORK_ERRORS;
  const sleep = options.sleep ?? wait;
  let current = job;
  let networkErrors = 0;
  options.onUpdate?.(current);

  while (current.status === "queued" || current.status === "running") {
    await sleep(intervalMs);
    try {
      current = await getJob<TResult>(current.id, options);
      networkErrors = 0;
    } catch (error) {
      if (error instanceof ApiError || ++networkErrors > maxNetworkErrors) throw error;
      continue;
    }
    options.onUpdate?.(current);
  }

  if (current.status === "failed" || !current.result) {
    throw new ApiError(current.error?.message || "任务没有成功完成。", 200, current.error?.code);
  }
  return current.result;
}

export async function submitRestore(
  image: File,
  scale: number,
  options: { origin?: string; fetcher?: Fetcher } = {},
): Promise<Job<RestoreResult>> {
  const origin = options.origin ?? defaultApiOrigin();
  const fetcher = options.fetcher ?? fetch;
  const formData = new FormData();
  formData.append("image", image);
  formData.append("scale", String(scale));
  const response = await decode<JobResponse<RestoreResult>>(
    await fetcher(resolveApiUrl("/api/restore", origin), {
      method: "POST",
      body: formData,
    }),
  );
  return response.job;
}

export async function submitAnimation(
  image: File,
  driver: AnimationDriver,
  options: { origin?: string; fetcher?: Fetcher } = {},
): Promise<Job<AnimateResult>> {
  const origin = options.origin ?? defaultApiOrigin();
  const fetcher = options.fetcher ?? fetch;
  const formData = new FormData();
  formData.append("image", image);
  if ("motionId" in driver) {
    formData.append("motionId", driver.motionId);
  } else {
    formData.append("drivingVideo", driver.drivingVideo);
  }
  const response = await decode<JobResponse<AnimateResult>>(
    await fetcher(resolveApiUrl("/api/animate", origin), {
      method: "POST",
      body: formData,
    }),
  );
  return response.job;
}

export async function restorePhoto(
  image: File,
  scale: number,
  options: JobWatchOptions<RestoreResult> = {},
): Promise<RestoreResult> {
  return waitForJob(await submitRestore(image, scale, options), options);
}

export async function animatePortrait(
  image: File,
  driver: AnimationDriver,
  options: JobWatchOptions<AnimateResult> = {},
): Promise<AnimateResult> {
  return waitForJob(await submitAnimation(image, driver, options), { intervalMs: 2_000, ...options });
}
