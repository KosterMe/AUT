import axios from "axios";
import type {
  Account,
  Clip,
  ClipJob,
  ClipJobDetail,
  CreateJobPayload,
  Health,
  MediaAsset,
  PreviewRequest,
  Publication,
  Scenario,
  ScenarioData,
  ScenarioInspect,
  ScenarioPayload,
  StyleDefaults,
  StyleGroups,
  StylePayload,
  StylePreset,
  Task,
  UploadOptions,
} from "./types";

export const api = axios.create({
  baseURL: "/api",
  headers: { "Content-Type": "application/json" },
});

/** The API returns {detail} for expected failures; surface that, not "Request failed". */
export function errorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail) && detail.length) {
      return detail.map((item: any) => item.msg ?? String(item)).join("; ");
    }
    return error.message;
  }
  return error instanceof Error ? error.message : String(error);
}

export const System = {
  health: () => api.get<Health>("/health").then((r) => r.data),
  tasks: (params?: { status?: string; kind?: string }) =>
    api.get<Task[]>("/tasks", { params }).then((r) => r.data),
  cancelTask: (id: number) => api.post(`/tasks/${id}/cancel`).then((r) => r.data),
  clipVideoUrl: (clipId: number) => `/api/clips/${clipId}/video`,
  clipCoverUrl: (clipId: number) => `/api/clips/${clipId}/cover`,
};

export const Accounts = {
  list: () => api.get<Account[]>("/accounts").then((r) => r.data),
  remove: (id: number) => api.delete(`/accounts/${id}`),
  rename: (id: number, display_name: string) =>
    api.patch<Account>(`/accounts/${id}`, { display_name }).then((r) => r.data),
  importFromDisk: () => api.post<Account[]>("/accounts/import-from-disk").then((r) => r.data),
  resync: () => api.post<Account[]>("/accounts/resync").then((r) => r.data),
};

export const Assets = {
  list: () => api.get<MediaAsset[]>("/assets").then((r) => r.data),
  upload: (file: File, tags: string) => {
    const form = new FormData();
    form.append("file", file);
    form.append("tags", tags);
    return api
      .post<MediaAsset>("/assets", form, {
        headers: { "Content-Type": "multipart/form-data" },
      })
      .then((r) => r.data);
  },
  setTags: (id: number, tags: string) =>
    api.patch<MediaAsset>(`/assets/${id}`, { tags }).then((r) => r.data),
  remove: (id: number) => api.delete(`/assets/${id}`),
  fileUrl: (id: number) => `/api/assets/${id}/file`,
};

export const Login = {
  importCookies: (username: string, cookies: string) =>
    api.post("/login/import", { username, cookies }).then((r) => r.data),
  importCookieFile: (username: string, file: File) => {
    const form = new FormData();
    form.append("username", username);
    form.append("file", file);
    return api
      .post("/login/import-file", form, { headers: { "Content-Type": "multipart/form-data" } })
      .then((r) => r.data);
  },
  startBrowser: (username: string) =>
    api.post("/login/browser", { username }).then((r) => r.data),
  get: (id: string) => api.get(`/login/${id}`).then((r) => r.data),
};

export const Jobs = {
  list: () => api.get<ClipJob[]>("/jobs").then((r) => r.data),
  get: (id: number) => api.get<ClipJobDetail>(`/jobs/${id}`).then((r) => r.data),
  create: (payload: CreateJobPayload) =>
    api.post<ClipJob>("/jobs", payload).then((r) => r.data),
  start: (id: number) => api.post<ClipJob>(`/jobs/${id}/start`).then((r) => r.data),
  cancel: (id: number) => api.post<ClipJob>(`/jobs/${id}/cancel`).then((r) => r.data),
  remove: (id: number) => api.delete(`/jobs/${id}`),
  setTitle: (id: number, custom_title: string) =>
    api.patch<ClipJob>(`/jobs/${id}/title`, { custom_title }).then((r) => r.data),
  setCaptionTags: (id: number, caption_tags: string) =>
    api.patch<ClipJob>(`/jobs/${id}/caption-tags`, { caption_tags }).then((r) => r.data),
  clips: (id: number, unpublishedOnly = false) =>
    api
      .get<Clip[]>(`/jobs/${id}/clips`, { params: { unpublished_only: unpublishedOnly } })
      .then((r) => r.data),
};

export const Publications = {
  list: (status?: string) =>
    api.get<Publication[]>("/publications", { params: { status } }).then((r) => r.data),
  scheduleClip: (
    clipId: number,
    body: { username: string; scheduled_at: string; caption?: string; options?: UploadOptions },
  ) => api.post<Publication>(`/publications/clip/${clipId}`, body).then((r) => r.data),
  scheduleJob: (
    jobId: number,
    body: {
      username: string;
      first_at: string;
      interval_minutes: number;
      options?: UploadOptions;
    },
  ) => api.post<Publication[]>(`/publications/job/${jobId}`, body).then((r) => r.data),
  update: (id: number, body: { scheduled_at?: string; caption?: string }) =>
    api.patch<Publication>(`/publications/${id}`, body).then((r) => r.data),
  cancel: (id: number) => api.post<Publication>(`/publications/${id}/cancel`).then((r) => r.data),
  cancelScheduled: (jobId?: number) =>
    api
      .post<Publication[]>("/publications/cancel-scheduled", null, { params: { job_id: jobId } })
      .then((r) => r.data),
  retry: (id: number) => api.post<Publication>(`/publications/${id}/retry`).then((r) => r.data),
  retryUntouched: (jobId?: number) =>
    api
      .post<Publication[]>("/publications/retry-untouched", null, { params: { job_id: jobId } })
      .then((r) => r.data),
  remove: (id: number) => api.delete(`/publications/${id}`),
};

export const Styles = {
  list: () => api.get<StylePreset[]>("/styles").then((r) => r.data),
  /**
   * Every value a profile renders with when no preset is attached. The editor
   * shows these behind empty controls, so a form nobody fills in still
   * describes exactly what will happen.
   */
  defaults: (profile?: string) =>
    api.get<StyleDefaults>("/styles/defaults", { params: { profile } }).then((r) => r.data),
  create: (body: StylePayload) => api.post<StylePreset>("/styles", body).then((r) => r.data),
  update: (id: number, body: StylePayload) =>
    api.patch<StylePreset>(`/styles/${id}`, body).then((r) => r.data),
  remove: (id: number) => api.delete(`/styles/${id}`),
};

export const Clips = {
  composition: (clipId: number) =>
    api
      .get<{ clip_id: number; composition: Record<string, unknown> }>(
        `/clips/${clipId}/composition`,
      )
      .then((r) => r.data),
  rerender: (clipId: number, body: { style_id?: number | null; style?: StyleGroups } = {}) =>
    api.post<Clip>(`/clips/${clipId}/render`, body).then((r) => r.data),
  /**
   * A few seconds of a clip in a given style, as a blob the browser can play.
   * The caller owns the object URL and has to revoke it — this is called every
   * time a control moves, and leaking one per keystroke adds up.
   */
  preview: async (clipId: number, body: PreviewRequest = {}) => {
    const response = await api.post(`/clips/${clipId}/preview`, body, {
      responseType: "blob",
    });
    return URL.createObjectURL(response.data as Blob);
  },
};

export const Scenarios = {
  list: () => api.get<Scenario[]>("/scenarios").then((r) => r.data),
  get: (id: number) => api.get<Scenario>(`/scenarios/${id}`).then((r) => r.data),
  create: (body: ScenarioPayload) =>
    api.post<Scenario>("/scenarios", body).then((r) => r.data),
  /**
   * Saving a built-in answers with a copy of it, under a new id. The caller
   * has to follow that id rather than keep writing to the one it addressed —
   * otherwise the next save makes a second copy.
   */
  update: (id: number, body: ScenarioPayload) =>
    api.put<Scenario>(`/scenarios/${id}`, body).then((r) => r.data),
  remove: (id: number) => api.delete(`/scenarios/${id}`),
  /**
   * What this scenario does on a clip of that length. The draft is sent
   * rather than the id: the editor asks on every edit, and what it needs laid
   * out is what is on screen, not what was last written down.
   */
  inspect: (data: ScenarioData, duration_sec?: number) =>
    api
      .post<ScenarioInspect>("/scenarios/inspect", { data, duration_sec })
      .then((r) => r.data),
  /** «Примерить»: the draft on one real clip, small and fast. */
  preview: async (
    id: number,
    body: { clip_id: number; data?: ScenarioData; at_sec?: number; duration_sec?: number },
  ) => {
    const response = await api.post(`/scenarios/${id}/preview`, body, {
      responseType: "blob",
    });
    return URL.createObjectURL(response.data as Blob);
  },
};
