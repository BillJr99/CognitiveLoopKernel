// Thin fetch wrapper that understands the CLK `{ok, ...}` /
// `{ok:false, error:{code,message}}` envelope. Same-origin by default;
// the Vite dev server proxies `/api` to the local FastAPI server.

export class ApiError extends Error {
  code: string;
  status: number;
  constructor(code: string, message: string, status: number) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

async function parse<T>(res: Response): Promise<T> {
  let body: any = null;
  try {
    body = await res.json();
  } catch {
    if (!res.ok) throw new ApiError("http_error", res.statusText, res.status);
    return {} as T;
  }
  if (body && body.ok === false) {
    const err = body.error || {};
    throw new ApiError(err.code || "error", err.message || "Request failed", res.status);
  }
  if (!res.ok) {
    throw new ApiError("http_error", res.statusText, res.status);
  }
  return body as T;
}

export async function apiGet<T>(path: string): Promise<T> {
  return parse<T>(await fetch(path, { headers: { Accept: "application/json" } }));
}

export async function apiSend<T>(path: string, method: string, body?: unknown): Promise<T> {
  return parse<T>(
    await fetch(path, {
      method,
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  );
}

export const apiPost = <T>(p: string, b?: unknown) => apiSend<T>(p, "POST", b);
export const apiPut = <T>(p: string, b?: unknown) => apiSend<T>(p, "PUT", b);
export const apiPatch = <T>(p: string, b?: unknown) => apiSend<T>(p, "PATCH", b);
export const apiDelete = <T>(p: string) => apiSend<T>(p, "DELETE");
