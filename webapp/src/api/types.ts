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

// --- scenarios ---------------------------------------------------------------
//
// A scenario is a montage written before the video exists. These mirror
// montage/scenario/store.py, and they are deliberately loose in one direction:
// almost every field is optional, because the server's decoder fills in the
// model's own defaults for anything left out. That is what makes it safe for
// the editor to write a partial element — it never has to state a default it
// did not mean to set, so there is no second copy of the defaults here to
// drift from the first.

export interface ScenarioKeyframe {
  /** Anchored rather than timed, like everything else about a scenario. */
  at: ScenarioAnchor;
  value: number;
  /** How the value *leaves* this key: linear | in | out | in_out | step. */
  easing?: string;
}

export interface ScenarioAnimated {
  static: number;
  keys?: ScenarioKeyframe[];
}

/** A number the editor sets, as the model stores it. */
export type Numeric = ScenarioAnimated | number;

export type SlotKind =
  | "source"
  | "source_at"
  | "library"
  | "upload"
  | "color"
  | "gradient"
  | "text"
  | "blur_of";

export type AnchorMode = "start" | "end" | "fraction" | "after" | "before" | "event";

export type DurationMode = "fixed" | "elastic" | "rest" | "until" | "natural";

export type TrackKind = "spine" | "video" | "audio" | "overlay";

export interface ScenarioSlot {
  kind: SlotKind;
  tag?: string;
  pick?: string;
  upload_id?: number | null;
  color?: string;
  template?: string;
  ref?: string;
  event?: unknown;
}

export interface ScenarioAnchor {
  mode: AnchorMode;
  value?: number;
  ref?: string | null;
  offset_sec?: number;
  event?: unknown;
}

export interface ScenarioDuration {
  mode: DurationMode;
  value?: number;
  grow?: number;
  min_sec?: number;
  max_sec?: number;
  until?: unknown;
}

export interface ScenarioFrame {
  /** Percent of the canvas, and x/y are the centre rather than the corner. */
  x?: Numeric;
  y?: Numeric;
  width?: Numeric;
  height?: Numeric;
  rotate?: Numeric;
  opacity?: Numeric;
  fit?: string;
  align?: string;
  radius?: number;
  crop?: unknown;
  blend?: string;
}

export interface ScenarioElement {
  id: string;
  slot?: ScenarioSlot;
  start?: ScenarioAnchor;
  duration?: ScenarioDuration;
  frame?: ScenarioFrame;
  effects?: unknown[];
  audio?: Record<string, unknown>;
  transition_in?: unknown;
  transition_out?: unknown;
  label?: string;
  optional?: boolean;
  priority?: number;
  /** Present only on rules. It is what makes an element one. */
  rule?: string;
  limit?: number;
  min_gap_sec?: number;
  guard_head_sec?: number;
  guard_tail_sec?: number;
  max_share?: number;
  params?: Record<string, unknown>;
  template?: ScenarioElement | null;
}

export interface ScenarioTrack {
  id: string;
  kind: TrackKind;
  z?: number;
  muted?: boolean;
  locked?: boolean;
  elements: ScenarioElement[];
}

export interface ScenarioData {
  version?: number;
  name?: string;
  canvas?: { width: number; height: number; fps: number };
  mock?: {
    duration_sec: number;
    width: number;
    height: number;
    cuts: number[];
    sample_clip_id: number | null;
  };
  style?: StyleGroups;
  tracks: ScenarioTrack[];
}

export interface Scenario {
  id: number;
  name: string;
  /** Which save this is. Send it back with an edit; a stale one is refused. */
  version: number;
  description: string;
  /** Ships with the service: it cannot be deleted, and an edit makes a copy. */
  builtin: boolean;
  data: ScenarioData;
  created_at: string;
  updated_at: string;
}

/**
 * What this build of ffmpeg can do, measured by the probe rather than assumed
 * (§7.2). `animatable` is keyed by the frame properties the editor knows, and
 * that mapping is made on the server, next to the renderer choosing the
 * filters — an editor holding its own copy would be a second list of names.
 */
export interface Capabilities {
  ok: boolean;
  build: string;
  detail: string;
  animatable: Record<string, boolean>;
}

export interface InspectRect {
  x: number;
  y: number;
  width: number;
  height: number;
  /** Degrees, sampled at the report's moment like everything else here. */
  rotate: number;
  /** 0..1, sampled the same way. */
  opacity: number;
  fit: string;
  /** One frame of a moving rectangle: sampled at the report's `at_sec`. */
  moving: boolean;
}

export interface InspectKey {
  property: string;
  at_sec: number;
  value: number;
  easing: string;
  anchor: string;
}

export interface InspectBlock {
  element_id: string;
  track_id: string;
  track_kind: TrackKind;
  label: string;
  slot_kind: SlotKind;
  slot_tag: string;
  at_sec: number;
  duration_sec: number;
  anchor: AnchorMode;
  frame: InspectRect;
  z: number;
  optional: boolean;
  /** False means it was dropped: it did not fit, or nothing could fill it. */
  placed: boolean;
  note: string;
  keys: InspectKey[];
}

export interface InspectGhost {
  kind: "layer" | "audio";
  at_sec: number;
  duration_sec: number;
  source_path: string;
}

export interface InspectRule {
  element_id: string;
  rule: string;
  track_id: string;
  label: string;
  limit: number;
  ghosts: InspectGhost[];
}

export interface InspectWarning {
  code: string;
  message: string;
  element_id: string;
}

export interface ScenarioInspect {
  scenario_id: number;
  name: string;
  /** What the clip offered, what the scenario laid out, what will be rendered. */
  material_sec: number;
  timeline_sec: number;
  duration_sec: number;
  canvas_width: number;
  canvas_height: number;
  layout: string;
  blocks: InspectBlock[];
  rules: InspectRule[];
  warnings: InspectWarning[];
  subtitle_count: number;
  /** The moment the rectangles above are for. */
  at_sec: number;
  durations: number[];
}

export interface ScenarioPayload {
  name?: string;
  description?: string | null;
  data: ScenarioData;
  /** The version this edit was made against. */
  version?: number;
}
