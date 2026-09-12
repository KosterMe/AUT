import { useEffect, useMemo, useRef, useState } from "react";
import { Eye, Loader2, Plus, RotateCcw, Save, Trash2 } from "lucide-react";
import clsx from "clsx";
import { Clips, Styles, errorMessage } from "../api/client";
import {
  keys,
  useInvalidatingMutation,
  useJob,
  useJobs,
  useStyleDefaults,
  useStyles,
} from "../api/hooks";
import type { JobProfile, StyleGroups, StylePreset } from "../api/types";
import { PROFILE_LABELS, STYLE_GROUPS, type StyleField } from "./styleFields";

/**
 * The look editor.
 *
 * Two things make it usable rather than a wall of forty inputs. Every control
 * shows the value that will actually be used, greyed, until somebody changes
 * it — so nothing has to be filled in for the page to describe what happens.
 * And the preview renders four seconds of a real clip with whatever is
 * currently on screen, saved or not, which is the difference between tuning a
 * look and guessing at one.
 */
export default function StylesPage() {
  const { data: presets, isLoading } = useStyles();
  const [selected, setSelected] = useState<number | "new">("new");

  const preset = presets?.find((item) => item.id === selected);

  return (
    <div className="space-y-6">
      <header>
        <h2 className="text-2xl font-semibold">Looks</h2>
        <p className="text-sm text-slate-500 mt-1">
          A look is whatever you have an opinion about — nothing else. Everything left
          alone follows the profile, and a job with no look at all still renders, which
          is what keeps this optional.
        </p>
      </header>

      <div className="flex flex-wrap gap-2">
        <button
          onClick={() => setSelected("new")}
          className={clsx("chip", selected === "new" && "bg-brand-50 text-brand-700")}
        >
          <Plus size={14} className="inline mr-1" />
          New
        </button>
        {isLoading && <span className="text-sm text-slate-500">Loading…</span>}
        {presets?.map((item) => (
          <button
            key={item.id}
            onClick={() => setSelected(item.id)}
            className={clsx("chip", selected === item.id && "bg-brand-50 text-brand-700")}
          >
            {item.name}
          </button>
        ))}
      </div>

      <StyleEditor
        key={preset?.id ?? "new"}
        preset={preset}
        onSaved={(id) => setSelected(id)}
        onDeleted={() => setSelected("new")}
      />
    </div>
  );
}

function StyleEditor({
  preset,
  onSaved,
  onDeleted,
}: {
  preset?: StylePreset;
  onSaved: (id: number) => void;
  onDeleted: () => void;
}) {
  const [name, setName] = useState(preset?.name ?? "");
  const [description, setDescription] = useState(preset?.description ?? "");
  const [profile, setProfile] = useState<JobProfile>(preset?.profile ?? "talking");
  const [overrides, setOverrides] = useState<StyleGroups>(preset?.data ?? {});
  const [error, setError] = useState("");

  const { data: defaults } = useStyleDefaults(profile);
  // What every control will do if nobody touches it: the profile's defaults
  // with this preset's overrides already applied.
  const effective = useMemo(
    () => merge(defaults?.style ?? {}, overrides),
    [defaults?.style, overrides],
  );

  const save = useInvalidatingMutation(
    async () => {
      const body = { name, description, profile, data: overrides };
      return preset ? Styles.update(preset.id, body) : Styles.create(body);
    },
    [keys.styles],
  );

  const remove = useInvalidatingMutation(
    async () => (preset ? Styles.remove(preset.id) : undefined),
    [keys.styles, keys.jobs],
  );

  function setField(group: string, key: string, value: unknown) {
    setOverrides((current) => ({ ...current, [group]: { ...current[group], [key]: value } }));
  }

  /** Stop overriding a field: it goes back to following the default. */
  function clearField(group: string, key: string) {
    setOverrides((current) => {
      const next = { ...current, [group]: { ...current[group] } };
      delete next[group][key];
      if (!Object.keys(next[group]).length) delete next[group];
      return next;
    });
  }

  const changed = Object.values(overrides).reduce(
    (total, group) => total + Object.keys(group).length,
    0,
  );

  return (
    <div className="grid grid-cols-1 xl:grid-cols-[1fr_360px] gap-6 items-start">
      <div className="space-y-4">
        <div className="card space-y-3">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className="label">Name</label>
              <input
                className="input"
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="House look"
              />
            </div>
            <div>
              <label className="label">Written for</label>
              <select
                className="input"
                value={profile}
                onChange={(event) => setProfile(event.target.value as JobProfile)}
              >
                {(Object.keys(PROFILE_LABELS) as JobProfile[]).map((id) => (
                  <option key={id} value={id}>
                    {PROFILE_LABELS[id]}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div>
            <label className="label">Note</label>
            <input
              className="input"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="What this is for"
            />
          </div>
          <div className="flex items-center gap-3 pt-1">
            <button
              className="btn btn-primary"
              disabled={!name.trim() || save.isPending}
              onClick={() =>
                save
                  .mutateAsync(undefined)
                  .then((saved) => {
                    setError("");
                    if (saved) onSaved(saved.id);
                  })
                  .catch((reason) => setError(errorMessage(reason)))
              }
            >
              <Save size={15} className="inline mr-1" />
              {preset ? "Save" : "Create"}
            </button>
            {preset && (
              <button
                className="btn btn-danger"
                onClick={() =>
                  remove.mutateAsync(undefined).then(onDeleted).catch((reason) =>
                    setError(errorMessage(reason)),
                  )
                }
              >
                <Trash2 size={15} className="inline mr-1" />
                Delete
              </button>
            )}
            <span className="text-xs text-slate-500">
              {changed === 0
                ? "Nothing overridden — this renders exactly like the profile."
                : `${changed} field${changed === 1 ? "" : "s"} overridden`}
            </span>
          </div>
          {error && <p className="text-sm text-red-600">{error}</p>}
        </div>

        {STYLE_GROUPS.map((group) => (
          <section key={group.key} className="card space-y-3">
            <div>
              <h3 className="font-medium">{group.title}</h3>
              <p className="text-xs text-slate-500 mt-1 leading-snug">{group.blurb}</p>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-3">
              {group.fields.map((field) => (
                <FieldRow
                  key={field.key}
                  field={field}
                  value={overrides[group.key]?.[field.key]}
                  fallback={effective[group.key]?.[field.key]}
                  onChange={(value) => setField(group.key, field.key, value)}
                  onClear={() => clearField(group.key, field.key)}
                />
              ))}
            </div>
          </section>
        ))}
      </div>

      <PreviewPanel style={effective} />
    </div>
  );
}

function FieldRow({
  field,
  value,
  fallback,
  onChange,
  onClear,
}: {
  field: StyleField;
  value: unknown;
  fallback: unknown;
  onChange: (value: unknown) => void;
  onClear: () => void;
}) {
  const overridden = value !== undefined;
  const shown = overridden ? value : fallback;

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between gap-2">
        <label className={clsx("label mb-0", !overridden && "text-slate-400")}>
          {field.label}
        </label>
        {overridden && (
          <button
            className="text-xs text-slate-400 hover:text-slate-700"
            onClick={onClear}
            title="Follow the default again"
          >
            <RotateCcw size={12} className="inline" />
          </button>
        )}
      </div>

      {field.kind === "toggle" ? (
        <button
          onClick={() => onChange(!shown)}
          className={clsx(
            "h-6 w-11 rounded-full transition-colors relative",
            shown ? "bg-brand-600" : "bg-slate-300",
            !overridden && "opacity-60",
          )}
        >
          <span
            className={clsx(
              "absolute top-0.5 h-5 w-5 rounded-full bg-white transition-all",
              shown ? "left-5" : "left-0.5",
            )}
          />
        </button>
      ) : field.kind === "select" ? (
        <select
          className={clsx("input", !overridden && "text-slate-400")}
          value={String(shown ?? "")}
          onChange={(event) => onChange(event.target.value)}
        >
          {field.options?.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      ) : field.kind === "colour" ? (
        <div className="flex items-center gap-2">
          <input
            type="color"
            className="h-8 w-12 rounded border border-slate-300"
            value={String(shown ?? "#ffffff")}
            onChange={(event) => onChange(event.target.value.toUpperCase())}
          />
          <span className="text-xs text-slate-500">{String(shown ?? "")}</span>
        </div>
      ) : (
        <input
          className={clsx("input", !overridden && "text-slate-400")}
          type={field.kind === "number" ? "number" : "text"}
          min={field.min}
          max={field.max}
          step={field.step}
          value={String(shown ?? "")}
          onChange={(event) =>
            onChange(
              field.kind === "number"
                ? Number(event.target.value)
                : event.target.value,
            )
          }
        />
      )}

      {field.hint && <p className="text-[11px] text-slate-400 leading-snug">{field.hint}</p>}
    </div>
  );
}

/**
 * Four seconds of a real clip, rendered with whatever is on screen right now.
 *
 * Unsaved on purpose: the point is to see a change before deciding to keep it.
 * The clip is picked from a job that already has one rendered, because that
 * guarantees its source is still on disk and its transcript is cached — the
 * two things a preview cannot wait for.
 */
function PreviewPanel({ style }: { style: StyleGroups }) {
  const { data: jobs } = useJobs();
  const candidates = useMemo(
    () => (jobs ?? []).filter((job) => job.status === "ready" || job.status === "rendering"),
    [jobs],
  );
  const [jobId, setJobId] = useState<number | null>(null);
  const chosenJob = jobId ?? candidates[0]?.id ?? null;
  const { data: job } = useJob(chosenJob ?? 0);

  const clips = (job?.clips ?? []).filter((clip) => clip.status === "ready");
  const [clipId, setClipId] = useState<number | null>(null);
  const chosenClip = clipId && clips.some((c) => c.id === clipId) ? clipId : clips[0]?.id ?? null;

  const [at, setAt] = useState(0);
  const [url, setUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const previous = useRef<string | null>(null);

  // Object URLs are per-render and never freed by the browser on their own;
  // this page makes one every time somebody presses the button.
  useEffect(() => {
    return () => {
      if (previous.current) URL.revokeObjectURL(previous.current);
    };
  }, []);

  async function render() {
    if (!chosenClip) return;
    setBusy(true);
    setError("");
    try {
      const next = await Clips.preview(chosenClip, { at_sec: at, duration_sec: 4, style });
      if (previous.current) URL.revokeObjectURL(previous.current);
      previous.current = next;
      setUrl(next);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card space-y-3 xl:sticky xl:top-6">
      <div>
        <h3 className="font-medium">Preview</h3>
        <p className="text-xs text-slate-500 mt-1 leading-snug">
          Four seconds of a rendered clip, with the settings above applied — saved or
          not. Composed by the same code the real render uses, only shorter and smaller.
        </p>
      </div>

      {candidates.length === 0 ? (
        <p className="text-sm text-slate-500">
          Nothing to preview on yet. Render a video first and this will pick one of its
          clips.
        </p>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-2">
            <select
              className="input"
              value={chosenJob ?? ""}
              onChange={(event) => {
                setJobId(Number(event.target.value));
                setClipId(null);
              }}
            >
              {candidates.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.custom_title || item.title || `Video ${item.id}`}
                </option>
              ))}
            </select>
            <select
              className="input"
              value={chosenClip ?? ""}
              onChange={(event) => setClipId(Number(event.target.value))}
            >
              {clips.map((clip) => (
                <option key={clip.id} value={clip.id}>
                  часть {clip.index}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="label">Start at {at.toFixed(0)}s</label>
            <input
              type="range"
              className="w-full"
              min={0}
              max={120}
              step={1}
              value={at}
              onChange={(event) => setAt(Number(event.target.value))}
            />
          </div>

          <button className="btn btn-primary w-full" onClick={render} disabled={busy || !chosenClip}>
            {busy ? (
              <Loader2 size={15} className="inline mr-1 animate-spin" />
            ) : (
              <Eye size={15} className="inline mr-1" />
            )}
            {busy ? "Rendering…" : "Render preview"}
          </button>

          {error && <p className="text-sm text-red-600">{error}</p>}

          {url && (
            <video
              key={url}
              src={url}
              controls
              autoPlay
              loop
              className="w-full rounded-lg bg-black aspect-[9/16]"
            />
          )}
        </>
      )}
    </div>
  );
}

/** Defaults with overrides laid on top, one level deep — the shape of a style. */
function merge(base: StyleGroups, overrides: StyleGroups): StyleGroups {
  const result: StyleGroups = {};
  for (const [group, values] of Object.entries(base)) {
    result[group] = { ...(values as Record<string, unknown>) };
  }
  for (const [group, values] of Object.entries(overrides)) {
    result[group] = { ...(result[group] ?? {}), ...values };
  }
  return result;
}
