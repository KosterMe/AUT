// Mirrors app/api/schemas. Kept narrow on purpose: the UI should break loudly
// at compile time when the API changes shape, not silently at runtime.

export type JobStatus =
  | "created"
  | "downloading"
  | "transcribing"
  | "planning"
  | "rendering"
  | "ready"
  | "failed"
  | "cancelled";

export type ClipStatus = "planned" | "rendering" | "ready" | "failed" | "cancelled";

// What kind of video a job is. It picks the cutter and the frame, so it is
// chosen once when the video is added rather than per render.
export type JobProfile = "talking" | "plain" | "split" | "film";

export type PublicationStatus =
  | "scheduled"
  | "publishing"
  | "published"
  | "failed"
  | "cancelled";

export type TaskStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface Account {
  id: number;
  username: string;
  display_name: string | null;
  has_valid_session: boolean;
  created_at: string;
  updated_at: string;
  last_used_at: string | null;
}

export interface Clip {
  id: number;
  job_id: number;
  index: number;
  start_sec: number;
  end_sec: number;
  duration_sec: number;
  title: string | null;
  text: string | null;
  video_path: string | null;
  cover_path: string | null;
  status: ClipStatus;
  error: string | null;
  created_at: string;
  updated_at: string;
  // Present when the clip is already spoken for by a publication.
  publication_id: number | null;
  publication_status: PublicationStatus | null;
}

export interface ClipJob {
  id: number;
  style_id: number | null;
  source_platform: string;
  source_ref: string;
  profile: JobProfile;
  title: string | null;
  custom_title: string | null;
  caption_tags: string | null;
  duration_seconds: number | null;
  status: JobStatus;
  stage: string | null;
  progress: number;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface ClipJobDetail extends ClipJob {
  clips: Clip[];
}

export interface Publication {
  id: number;
  account_id: number;
  clip_id: number | null;
  source_kind: string;
  source_ref: string;
  caption: string;
  scheduled_at: string;
  status: PublicationStatus;
  result_text: string | null;
  published_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface Task {
  id: number;
  kind: string;
  status: TaskStatus;
  stage: string;
  progress: number;
  attempts: number;
  max_attempts: number;
  run_at: string;
  error: string | null;
  payload: Record<string, unknown>;
}

export interface Health {
  status: string;
  version: string;
  environment: string;
  tasks: Partial<Record<TaskStatus, number>>;
  tasks_due_now: number;
  /** False where the local-browser login cannot work — a container, or a host
   *  without undetected-chromedriver. Importing cookies is the path there. */
  browser_login: boolean;
}

export interface MediaAsset {
  id: number;
  kind: "video" | "image" | "audio";
  original_name: string;
  tags: string[];
  duration_sec: number | null;
  width: number | null;
  height: number | null;
  size_bytes: number;
  last_used_at: string | null;
  use_count: number;
  created_at: string;
  updated_at: string;
}

export interface UploadOptions {
  allow_comment: 0 | 1;
  allow_duet: 0 | 1;
  allow_stitch: 0 | 1;
  visibility_type: 0 | 1;
  brand_organic_type: 0 | 1;
  branded_content_type: 0 | 1;
  ai_label: 0 | 1;
  proxy: string;
}

export const defaultUploadOptions: UploadOptions = {
  allow_comment: 1,
  allow_duet: 0,
  allow_stitch: 0,
  visibility_type: 0,
  brand_organic_type: 0,
  branded_content_type: 0,
  ai_label: 0,
  proxy: "",
};

export interface CreateJobPayload {
  source_ref: string;
  source_platform?: "auto" | "youtube" | "local";
  profile?: JobProfile;
  /** A saved look. Omitted means the profile's own defaults. */
  style_id?: number | null;
  custom_title?: string | null;
  caption_tags?: string | null;
  start_immediately?: boolean;
  min_clip_seconds?: number;
  max_clip_seconds?: number;
  max_clips?: number;
}

export const JOB_PROFILES: { id: JobProfile; name: string; hint: string }[] = [
  {
    id: "talking",
    name: "Talking",
    hint: "Podcasts and interviews. Cuts on sentence ends, drops the pauses, lays b-roll over the talk.",
  },
  {
    id: "plain",
    name: "Plain",
    hint: "The same cuts, nothing added and nothing removed. For material that is already edited.",
  },
  {
    id: "split",
    name: "Split screen",
    hint: "Speaker on top, filler footage below — from a library asset tagged 'background'.",
  },
  {
    id: "film",
    name: "Film",
    hint: "Films, shows and sport. Cuts where the picture cuts and needs no speech at all.",
  },
];

// A job is finished with the queue once it reaches one of these; the UI stops
// polling at that point.
export const TERMINAL_JOB_STATUSES: JobStatus[] = ["ready", "failed", "cancelled"];

// --- styles -----------------------------------------------------------------

/**
 * A style is a nested bag of scalars: framing, grade, subtitles, pacing,
 * inserts, audio, delivery. It is deliberately untyped per field here — the
 * server owns the schema, and the editor is driven by a descriptor table, so
 * mirroring forty field names in TypeScript would only add a second place to
 * forget one.
 */
export type StyleGroups = Record<string, Record<string, unknown>>;

export interface StylePreset {
  id: number;
  name: string;
  description: string;
  profile: JobProfile | null;
  /** Only what this preset overrides. Everything else follows the defaults. */
  data: StyleGroups;
  /** Those overrides with the defaults filled in — what a clip would render as. */
  resolved: StyleGroups;
  created_at: string;
  updated_at: string;
}

export interface StyleDefaults {
  profile: JobProfile;
  style: StyleGroups;
  profiles: JobProfile[];
}

export interface StylePayload {
  name?: string;
  description?: string;
  profile?: JobProfile | null;
  data?: StyleGroups;
}

export interface PreviewRequest {
  at_sec?: number;
  duration_sec?: number;
  scale?: number;
  style_id?: number | null;
  style?: StyleGroups;
}
