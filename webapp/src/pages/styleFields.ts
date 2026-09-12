import type { JobProfile } from "../api/types";

/**
 * What the style editor shows, and in what order.
 *
 * A descriptor table rather than a hand-written form: every control here is
 * the same shape — a value, a default behind it, and a way to stop overriding
 * it — so describing them as data is what keeps forty fields from becoming
 * forty hand-maintained inputs that drift apart.
 *
 * Nothing in this table is required. A field the operator never touches is
 * simply absent from what gets saved, and the server fills it in from the
 * profile and the defaults.
 */
export type FieldKind = "number" | "toggle" | "text" | "select" | "colour";

export interface StyleField {
  key: string;
  label: string;
  kind: FieldKind;
  min?: number;
  max?: number;
  step?: number;
  options?: { value: string; label: string }[];
  hint?: string;
}

export interface StyleGroup {
  key: string;
  title: string;
  blurb: string;
  fields: StyleField[];
}

export const STYLE_GROUPS: StyleGroup[] = [
  {
    key: "framing",
    title: "Frame",
    blurb:
      "How the source is fitted into a 9:16 screen. “Auto” crops anything already vertical and gives everything else a blurred backdrop made of itself.",
    fields: [
      {
        key: "layout",
        label: "Layout",
        kind: "select",
        options: [
          { value: "auto", label: "Auto — decide from the source" },
          { value: "blur", label: "Blurred backdrop" },
          { value: "fill", label: "Crop to fill" },
          { value: "split", label: "Split screen" },
        ],
      },
      {
        key: "zoom",
        label: "Zoom",
        kind: "number",
        min: 1,
        max: 2,
        step: 0.05,
        hint: "How much of the frame the picture fills, at the cost of its own left and right edges. Past ~1.35 it starts cutting faces near the edge.",
      },
      {
        key: "blur_divisor",
        label: "Blur shortcut",
        kind: "number",
        min: 1,
        max: 8,
        step: 1,
        hint: "The backdrop is blurred at 1/n of the output size and scaled up. 4 is twice as fast as 1 and looks the same.",
      },
      { key: "blur_radius", label: "Blur radius", kind: "number", min: 1, max: 128, step: 1 },
      {
        key: "companion_tag",
        label: "Split screen tag",
        kind: "text",
        hint: "Which library tag fills the bottom half. Only used by the split layout.",
      },
    ],
  },
  {
    key: "subtitles",
    title: "Subtitles",
    blurb:
      "One word per cue, on the word’s own onset. The font is the bundled display face — the same one the headline uses.",
    fields: [
      { key: "enabled", label: "Burn subtitles", kind: "toggle" },
      {
        key: "font",
        label: "Font",
        kind: "text",
        hint: "A face libass can find. Oswald ships with the app and covers Cyrillic.",
      },
      { key: "font_size", label: "Size", kind: "number", min: 24, max: 200, step: 2 },
      {
        key: "position_percent",
        label: "Height on screen",
        kind: "number",
        min: 20,
        max: 95,
        step: 1,
        hint: "Percent down the frame. 76 sits just below the middle, clear of TikTok’s own overlay.",
      },
      { key: "colour", label: "Colour", kind: "colour" },
      { key: "outline_colour", label: "Outline", kind: "colour" },
      { key: "outline_ratio", label: "Outline weight", kind: "number", min: 0, max: 0.5, step: 0.01 },
      { key: "uppercase", label: "Uppercase", kind: "toggle" },
      { key: "strip_punctuation", label: "Drop punctuation", kind: "toggle" },
      {
        key: "animate",
        label: "Pop each word in",
        kind: "toggle",
        hint: "Off gives a hard cut between words.",
      },
      {
        key: "time_offset_seconds",
        label: "Timing offset",
        kind: "number",
        min: -1,
        max: 1,
        step: 0.05,
        hint: "Negative shows cues earlier. Whisper runs late on fast speech.",
      },
    ],
  },
  {
    key: "pacing",
    title: "Pauses",
    blurb:
      "Cutting the silence out. The guard at the bottom is what stops it doing damage: if it would remove almost nothing, or leave less than half the clip, the whole montage is abandoned and the clip plays continuously.",
    fields: [
      { key: "remove_silence", label: "Cut the pauses", kind: "toggle" },
      {
        key: "noise_db",
        label: "Silence below",
        kind: "number",
        min: -90,
        max: 0,
        step: 1,
        hint: "dB. An absolute level, so a quietly recorded source needs this raised.",
      },
      { key: "min_silence_seconds", label: "Shortest pause to cut", kind: "number", min: 0.05, max: 10, step: 0.05 },
      {
        key: "padding_seconds",
        label: "Breathing room",
        kind: "number",
        min: 0,
        max: 2,
        step: 0.01,
        hint: "Left on each side of a cut, so a word is never clipped.",
      },
      { key: "min_segment_seconds", label: "Shortest piece kept", kind: "number", min: 0.1, max: 30, step: 0.1 },
      { key: "min_kept_share", label: "Keep at least", kind: "number", min: 0, max: 1, step: 0.05 },
    ],
  },
  {
    key: "inserts",
    title: "B-roll",
    blurb:
      "Fragments from the library, placed on the words that name them. Every number is a guardrail rather than a target — the failure to design against is a hook buried under stock footage.",
    fields: [
      { key: "enabled", label: "Use b-roll", kind: "toggle" },
      {
        key: "kind",
        label: "Style",
        kind: "select",
        options: [
          { value: "broll_full", label: "Full frame" },
          { value: "broll_pip", label: "Corner box" },
        ],
      },
      { key: "max_inserts", label: "Most per clip", kind: "number", min: 0, max: 24, step: 1 },
      { key: "min_seconds", label: "Shortest", kind: "number", min: 0.3, max: 15, step: 0.1 },
      { key: "max_seconds", label: "Longest", kind: "number", min: 0.5, max: 30, step: 0.1 },
      {
        key: "min_gap_seconds",
        label: "Gap between",
        kind: "number",
        min: 0,
        max: 60,
        step: 0.5,
        hint: "Without it a passage dense in keywords becomes a slideshow.",
      },
      {
        key: "hook_guard_seconds",
        label: "Leave the hook alone",
        kind: "number",
        min: 0,
        max: 30,
        step: 0.5,
        hint: "The opening seconds decide whether anybody watches the rest.",
      },
      { key: "max_share", label: "Cover at most", kind: "number", min: 0, max: 0.9, step: 0.05 },
      { key: "cadence_when_no_match", label: "Fall back to a beat", kind: "toggle" },
    ],
  },
  {
    key: "audio",
    title: "Sound",
    blurb:
      "A bed tagged music, ducked under the speech, and sounds tagged sfx on the cuts. Both do nothing until the library has one.",
    fields: [
      { key: "music", label: "Music bed", kind: "toggle" },
      { key: "sfx", label: "Transition sounds", kind: "toggle" },
      {
        key: "music_gain_db",
        label: "Bed level",
        kind: "number",
        min: -60,
        max: 0,
        step: 1,
        hint: "How far below the rest of the mix it sits before ducking.",
      },
      { key: "duck_ratio", label: "Duck ratio", kind: "number", min: 1, max: 20, step: 0.5 },
      { key: "duck_release_ms", label: "Duck release", kind: "number", min: 10, max: 3000, step: 10 },
      { key: "effect_gain_db", label: "Effect level", kind: "number", min: -60, max: 12, step: 1 },
      { key: "max_effects", label: "Most effects per clip", kind: "number", min: 0, max: 40, step: 1 },
    ],
  },
  {
    key: "grade",
    title: "Look",
    blurb: "The pass over the finished frame. Sharpening is the quickest trade of crispness for speed there is.",
    fields: [
      { key: "contrast", label: "Contrast", kind: "number", min: 0.5, max: 2, step: 0.01 },
      { key: "saturation", label: "Saturation", kind: "number", min: 0, max: 3, step: 0.01 },
      { key: "sharpen", label: "Sharpen", kind: "toggle" },
      { key: "sharpen_luma", label: "Sharpen amount", kind: "number", min: 0, max: 3, step: 0.05 },
    ],
  },
  {
    key: "delivery",
    title: "File",
    blurb: "What comes out. Leave these alone unless you know why.",
    fields: [
      { key: "width", label: "Width", kind: "number", min: 360, max: 2160, step: 2 },
      { key: "height", label: "Height", kind: "number", min: 640, max: 3840, step: 2 },
      { key: "fps", label: "Frame rate", kind: "number", min: 15, max: 60, step: 1 },
      {
        key: "crf",
        label: "Quality (CRF)",
        kind: "number",
        min: 16,
        max: 35,
        step: 1,
        hint: "Lower is better and bigger. 23 is the default.",
      },
      {
        key: "strategy",
        label: "Compile as",
        kind: "select",
        options: [
          { value: "", label: "Follow the server setting" },
          { value: "one_pass", label: "One pass — fastest, best quality" },
          { value: "two_stage", label: "Two stages — cacheable segments" },
        ],
      },
    ],
  },
];

export const PROFILE_LABELS: Record<JobProfile, string> = {
  talking: "Talking",
  plain: "Plain",
  split: "Split screen",
  film: "Film",
};
