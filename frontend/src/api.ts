let csrfToken: string | null = null;

export function setCsrfToken(token: string | null) {
  csrfToken = token;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

type Options = { method?: string; json?: unknown; body?: BodyInit };

async function errorMessage(r: Response): Promise<string> {
  try {
    const j = await r.json();
    if (typeof j.detail === "string") return j.detail;
    if (Array.isArray(j.detail)) return j.detail.map((d: { msg?: string }) => d.msg).join("; ");
  } catch {
    /* non-JSON error body */
  }
  return r.statusText || `HTTP ${r.status}`;
}

export async function api<T = unknown>(path: string, opts: Options = {}): Promise<T> {
  const method = opts.method ?? "GET";
  const headers: Record<string, string> = {};
  let body = opts.body;
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.json);
  }
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const r = await fetch(path, { method, headers, body, credentials: "same-origin" });
  if (!r.ok) {
    if (r.status === 401) window.dispatchEvent(new Event("homeslice:unauthorized"));
    throw new ApiError(r.status, await errorMessage(r));
  }
  if (r.status === 204) return undefined as T;
  const type = r.headers.get("content-type") ?? "";
  return (type.includes("json") ? r.json() : r.text()) as Promise<T>;
}

export function uploadModel(file: File, onProgress: (pct: number) => void): Promise<Model> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/library?filename=${encodeURIComponent(file.name)}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    if (csrfToken) xhr.setRequestHeader("X-CSRF-Token", csrfToken);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(Math.round((e.loaded / e.total) * 100));
    xhr.onload = () => {
      let data: { detail?: string } & Partial<Model> = {};
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        /* ignore */
      }
      if (xhr.status === 201) resolve(data as Model);
      else reject(new ApiError(xhr.status, data.detail ?? `Upload failed (${xhr.status})`));
    };
    xhr.onerror = () => reject(new ApiError(0, "Network error"));
    xhr.send(file);
  });
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 ** 2).toFixed(1)} MB`;
}

export interface AuthStatus {
  setup_required: boolean;
  authenticated: boolean;
  username: string | null;
  csrf_token: string | null;
}

export interface Model {
  id: string;
  filename: string;
  ext: string;
  size: number;
  created_at: number;
  source_url: string | null;
  license: string | null;
  has_thumbnail: boolean;
  // Saved plates of a 3MF project (Bambu Studio / OrcaSlicer).
  plates?: { index: number; name: string; objects: number }[];
}

export interface Preset {
  name: string;
  kind: string;
  source: "user" | "system";
  inherits: string | null;
}

export interface ErrorInfo {
  title: string;
  cause: string;
  fix: string;
  code: number | null;
  raw: string | null;
}

export interface Job {
  id: string;
  status: "queued" | "running" | "done" | "failed" | "cancelled";
  stage: string | null;
  title: string | null;
  owner: string | null;
  error: string | null;
  error_info: ErrorInfo | null;
  // Totals across beds; multi-bed jobs also carry "plates" and "per_plate".
  stats: (Record<string, string> & { plates?: number; per_plate?: Record<string, string>[] }) | null;
  params: {
    model_ids: string[];
    copies?: number[];
    model_plates?: number[];
    plate_selection?: number[][];
    printer: string;
    process: string;
    filaments: string[];
    overrides?: Record<string, string>;
    arrange?: boolean;
    orient?: boolean;
  };
  models: { id: string; filename: string | null; has_thumbnail: boolean }[];
  files: string[];
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  duration: number | null;
}

export interface JobPage {
  items: Job[];
  total: number;
  printers: string[];
}

export interface Printer {
  id: string;
  name: string;
  kind: string;
  url: string;
  address: string | null;
  has_api_key: boolean;
  printer_preset: string | null;
  default_process: string | null;
  default_filaments: string[];
  default_bed_type: string | null;
  bed_coverage: number;
}

export interface PrinterStatus {
  state: string;
  filename: string | null;
  progress: number | null;
  message: string | null;
  address: string | null;
}

export interface Temps {
  temp: number | null;
  target: number | null;
}

export interface LivePrinter {
  id: string;
  name: string;
  connected: boolean;
  error: string | null;
  state: string;
  filename?: string | null;
  message?: string | null;
  progress?: number | null;
  elapsed?: number | null;
  remaining?: number | null;
  extruder?: Temps | null;
  bed?: Temps | null;
  address?: string | null;
  webcams: string[];
  needs_attention: boolean;
  attention_reason: string | null;
}

export interface QueueItem {
  id: string;
  printer_id: string;
  printer_name: string;
  job_id: string;
  job_title: string | null;
  file: string;
  position: number;
  status: "waiting" | "sending" | "sent" | "failed" | "cancelled";
  error: string | null;
  owner: string | null;
  created_at: number;
  sent_at: number | null;
}

export interface ActivityItem {
  id: number;
  at: number;
  actor: string | null;
  action: string;
  detail: string | null;
}

export interface ImportFile {
  id: string;
  name: string;
  size: number | null;
  preview: string | null;
  importable: boolean;
}

export interface ImportPreview {
  source: "printables" | "makerworld";
  id: string;
  url: string;
  title: string;
  author: string | null;
  license: string | null;
  images: string[];
  files: ImportFile[];
  downloadable: boolean;
  note: string | null;
}

export interface ReleaseInfo {
  tag: string;
  name: string;
  published_at: string;
  prerelease: boolean;
  html_url: string;
  notes: string;
  asset_name: string | null;
  asset_sha256: string | null;
  version_id: string;
  channel?: string;
}

export interface OrcaStatus {
  current: string | null;
  previous: string | null;
  installed: { id: string; current: boolean; tag?: string; installed_at?: number }[];
  channel: string;
  channels: string[];
  auto_update: boolean;
  latest: ReleaseInfo | null;
  progress: { state: string; version?: string };
  last_error: string | null;
}
