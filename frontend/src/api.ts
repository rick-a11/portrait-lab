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
};

export type RestoreResponse = {
  job: { id: string; status: "completed" };
  model: ModelStatus;
  message?: string;
  sourceUrl?: string;
  resultUrl: string;
};

export type AnimateResponse = {
  job: { id: string; status: "completed" };
  animation: ModelStatus;
  message?: string;
  resultUrl: string;
};

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

const LOCAL_API_PROBE_TIMEOUT_MS = 1_500;

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

export async function restorePhoto(
  image: File,
  scale: number,
  options: { origin?: string; fetcher?: Fetcher } = {},
): Promise<RestoreResponse> {
  const origin = options.origin ?? defaultApiOrigin();
  const fetcher = options.fetcher ?? fetch;
  const formData = new FormData();
  formData.append("image", image);
  formData.append("scale", String(scale));
  return decode<RestoreResponse>(
    await fetcher(resolveApiUrl("/api/restore", origin), {
      method: "POST",
      body: formData,
    }),
  );
}

export async function animatePortrait(
  image: File,
  driver: AnimationDriver,
  options: { origin?: string; fetcher?: Fetcher } = {},
): Promise<AnimateResponse> {
  const origin = options.origin ?? defaultApiOrigin();
  const fetcher = options.fetcher ?? fetch;
  const formData = new FormData();
  formData.append("image", image);
  if ("motionId" in driver) {
    formData.append("motionId", driver.motionId);
  } else {
    formData.append("drivingVideo", driver.drivingVideo);
  }
  return decode<AnimateResponse>(
    await fetcher(resolveApiUrl("/api/animate", origin), {
      method: "POST",
      body: formData,
    }),
  );
}
