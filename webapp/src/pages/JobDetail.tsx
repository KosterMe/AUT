import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  ArrowLeft,
  CalendarCheck,
  CalendarPlus,
  Palette,
  RefreshCw,
  Trash2,
  XCircle,
} from "lucide-react";
import { Clips, Jobs, Publications, System, errorMessage } from "../api/client";
import {
  keys,
  useAccounts,
  useInvalidatingMutation,
  useJob,
  useStyles,
} from "../api/hooks";
import StatusBadge from "../components/StatusBadge";
import { formatDuration } from "./Jobs";
import type { Clip } from "../api/types";

export default function JobDetailPage() {
  const { jobId } = useParams();
  const id = Number(jobId);
  const navigate = useNavigate();
  const { data: job, isLoading } = useJob(id);

  const cancel = useInvalidatingMutation(() => Jobs.cancel(id), [keys.job(id), keys.jobs]);
  const remove = useInvalidatingMutation(() => Jobs.remove(id), [keys.jobs]);

  if (isLoading) return <p className="text-sm text-slate-500">Loading…</p>;
  if (!job) return <p className="text-sm text-red-600">Video not found.</p>;

  const busy = !["ready", "failed", "cancelled"].includes(job.status);
  const ready = job.clips.filter((clip) => clip.status === "ready");
  // Only clips nobody has claimed yet can be scheduled in bulk.
  const schedulable = ready.filter((clip) => !clip.publication_id);

  return (
    <div className="space-y-6">
      <Link to="/jobs" className="inline-flex items-center gap-1 text-sm text-slate-500 hover:text-slate-800">
        <ArrowLeft size={16} /> All videos
      </Link>

      <header className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h2 className="text-2xl font-semibold truncate">
              {job.custom_title || job.title || job.source_ref}
            </h2>
            <StatusBadge status={job.status} />
          </div>
          <p className="text-sm text-slate-500 mt-1 truncate">{job.source_ref}</p>
          {job.error && <p className="text-sm text-red-600 mt-1">{job.error}</p>}
        </div>
        <div className="flex gap-2 shrink-0">
          {busy && (
            <button className="btn-secondary" onClick={() => cancel.mutate(undefined)}>
              <XCircle size={16} />
              Stop
            </button>
          )}
          {!busy && (
            <button
              className="btn-secondary text-red-600"
              onClick={() => {
                if (confirm("Delete this video and its clips?")) {
                  remove.mutate(undefined, { onSuccess: () => navigate("/jobs") });
                }
              }}
            >
              <Trash2 size={16} />
              Delete
            </button>
          )}
        </div>
      </header>

      {busy && (
        <div className="card">
          <div className="flex items-center justify-between text-sm mb-2">
            <span className="text-slate-600">{job.stage ?? job.status}</span>
            <span className="text-slate-500">{Math.round(job.progress * 100)}%</span>
          </div>
          <div className="h-2 rounded-full bg-slate-100 overflow-hidden">
            <div
              className="h-full bg-brand-500 transition-all"
              style={{ width: `${Math.max(3, job.progress * 100)}%` }}
            />
          </div>
        </div>
      )}

      {schedulable.length > 0 && <ScheduleAll jobId={id} count={schedulable.length} />}

      <section className="space-y-3">
        <h3 className="font-medium">
          Clips{" "}
          <span className="text-slate-400 font-normal">
            {job.clips.length > 0 && `(${ready.length} of ${job.clips.length} ready)`}
          </span>
        </h3>
        {job.clips.length === 0 && (
          <div className="card text-center text-slate-500 py-8">
            No clips yet — they appear once the video has been transcribed.
          </div>
        )}
        <div className="grid grid-cols-2 gap-4">
          {job.clips.map((clip) => (
            <ClipCard key={clip.id} clip={clip} />
          ))}
        </div>
      </section>
    </div>
  );
}

function ClipCard({ clip }: { clip: Clip }) {
  const [open, setOpen] = useState(false);
  const isReady = clip.status === "ready";

  return (
    <div className="card space-y-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-xs text-slate-400">Part {clip.index}</p>
          <p className="font-medium text-sm truncate">{clip.title ?? `Clip ${clip.index}`}</p>
        </div>
        <StatusBadge status={clip.status} title={clip.error ?? undefined} />
      </div>

      <p className="text-xs text-slate-500">
        {formatDuration(clip.start_sec)} – {formatDuration(clip.end_sec)} ·{" "}
        {Math.round(clip.duration_sec)}s
      </p>

      {clip.error && <p className="text-xs text-red-600">{clip.error}</p>}

      {isReady && (
        <>
          <video
            src={System.clipVideoUrl(clip.id)}
            poster={clip.cover_path ? System.clipCoverUrl(clip.id) : undefined}
            controls
            preload="none"
            className="w-full rounded-md bg-black aspect-[9/16] max-h-72 object-contain"
          />
          {clip.publication_id ? (
            <Link
              to="/publications"
              className="btn-secondary w-full justify-center"
              title="This clip is already queued for publishing"
            >
              <CalendarCheck size={16} />
              {clip.publication_status}
            </Link>
          ) : open ? (
            <ScheduleClip clip={clip} onDone={() => setOpen(false)} />
          ) : (
            <button className="btn-secondary w-full justify-center" onClick={() => setOpen(true)}>
              <CalendarPlus size={16} />
              Schedule
            </button>
          )}
          <RerenderClip clip={clip} />
        </>
      )}
    </div>
  );
}

/**
 * Render this one clip again, optionally into a different look.
 *
 * One clip rather than the job, because replanning a job recuts every clip in
 * it — the wrong tool for "I want this one to look different". The boundaries
 * stay where they are and only the render repeats, which is cheap: the
 * transcript is cached and the segments may still be in the fragment cache.
 */
function RerenderClip({ clip }: { clip: Clip }) {
  const { data: presets } = useStyles();
  const [open, setOpen] = useState(false);
  const [styleId, setStyleId] = useState<string>("");
  const [error, setError] = useState("");

  const rerender = useInvalidatingMutation(
    () => Clips.rerender(clip.id, styleId ? { style_id: Number(styleId) } : {}),
    [keys.job(clip.job_id), keys.jobs, keys.tasks],
  );

  if (!open) {
    return (
      <button
        className="text-xs text-slate-500 hover:text-slate-800 w-full text-center"
        onClick={() => setOpen(true)}
      >
        <RefreshCw size={12} className="inline mr-1" />
        Render again
      </button>
    );
  }

  return (
    <div className="space-y-2 border-t border-slate-100 pt-3">
      <label className="label">
        <Palette size={12} className="inline mr-1" />
        Look
      </label>
      <select
        className="input"
        value={styleId}
        onChange={(event) => setStyleId(event.target.value)}
      >
        <option value="">Keep the one it has</option>
        {presets?.map((preset) => (
          <option key={preset.id} value={preset.id}>
            {preset.name}
          </option>
        ))}
      </select>
      {error && <p className="text-xs text-red-600">{error}</p>}
      <div className="flex gap-2">
        <button
          className="btn btn-primary flex-1 justify-center"
          disabled={rerender.isPending}
          onClick={() =>
            rerender
              .mutateAsync(undefined)
              .then(() => setOpen(false))
              .catch((reason) => setError(errorMessage(reason)))
          }
        >
          {rerender.isPending ? "Queueing…" : "Render"}
        </button>
        <button className="btn btn-secondary" onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
    </div>
  );
}

function ScheduleClip({ clip, onDone }: { clip: Clip; onDone: () => void }) {
  const { data: accounts } = useAccounts();
  const usable = accounts?.filter((a) => a.has_valid_session) ?? [];
  const [username, setUsername] = useState("");
  const [when, setWhen] = useState(defaultLocalDateTime(30));
  const [error, setError] = useState<string | null>(null);

  const schedule = useInvalidatingMutation(
    (body: { username: string; scheduled_at: string }) =>
      Publications.scheduleClip(clip.id, body),
    [keys.job(clip.job_id), keys.publications(), keys.jobs],
  );

  const account = username || usable[0]?.username || "";

  return (
    <div className="space-y-2 border-t border-slate-100 pt-3">
      <select className="input" value={account} onChange={(e) => setUsername(e.target.value)}>
        {usable.length === 0 && <option value="">No account with a valid session</option>}
        {usable.map((a) => (
          <option key={a.id} value={a.username}>
            {a.username}
          </option>
        ))}
      </select>
      <input
        type="datetime-local"
        className="input"
        value={when}
        onChange={(e) => setWhen(e.target.value)}
      />
      {error && <p className="text-xs text-red-600">{error}</p>}
      <div className="flex gap-2">
        <button
          className="btn-primary flex-1"
          disabled={!account || schedule.isPending}
          onClick={() =>
            schedule.mutate(
              { username: account, scheduled_at: toIso(when) },
              { onSuccess: onDone, onError: (err) => setError(errorMessage(err)) },
            )
          }
        >
          Confirm
        </button>
        <button className="btn-secondary" onClick={onDone}>
          Cancel
        </button>
      </div>
    </div>
  );
}

function ScheduleAll({ jobId, count }: { jobId: number; count: number }) {
  const { data: accounts } = useAccounts();
  const usable = useMemo(
    () => accounts?.filter((a) => a.has_valid_session) ?? [],
    [accounts],
  );
  const [username, setUsername] = useState("");
  const [firstAt, setFirstAt] = useState(defaultLocalDateTime(30));
  const [interval, setInterval] = useState(60);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<number | null>(null);

  const schedule = useInvalidatingMutation(
    (body: { username: string; first_at: string; interval_minutes: number }) =>
      Publications.scheduleJob(jobId, body),
    [keys.job(jobId), keys.publications(), keys.jobs],
  );

  const account = username || usable[0]?.username || "";

  return (
    <div className="card space-y-4">
      <div>
        <h3 className="font-medium">Schedule all ready clips</h3>
        <p className="text-xs text-slate-500 mt-1">
          Clips are spaced apart rather than posted at once — bursts hit rate limits and
          read as spam.
        </p>
      </div>

      <div className="grid grid-cols-3 gap-4">
        <div>
          <label className="label">Account</label>
          <select className="input" value={account} onChange={(e) => setUsername(e.target.value)}>
            {usable.length === 0 && <option value="">No account available</option>}
            {usable.map((a) => (
              <option key={a.id} value={a.username}>
                {a.username}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="label">First post</label>
          <input
            type="datetime-local"
            className="input"
            value={firstAt}
            onChange={(e) => setFirstAt(e.target.value)}
          />
        </div>
        <div>
          <label className="label">Every (minutes)</label>
          <input
            type="number"
            className="input"
            min={1}
            max={1440}
            value={interval}
            onChange={(e) => setInterval(Number(e.target.value))}
          />
        </div>
      </div>

      {error && <p className="text-sm text-red-600">{error}</p>}
      {done !== null && (
        <p className="text-sm text-emerald-700">Scheduled {done} clip(s).</p>
      )}

      <button
        className="btn-primary"
        disabled={!account || schedule.isPending}
        onClick={() => {
          setError(null);
          setDone(null);
          schedule.mutate(
            { username: account, first_at: toIso(firstAt), interval_minutes: interval },
            {
              onSuccess: (rows) => setDone(rows.length),
              onError: (err) => setError(errorMessage(err)),
            },
          );
        }}
      >
        <CalendarPlus size={16} />
        {schedule.isPending ? "Scheduling…" : `Schedule ${count} clip(s)`}
      </button>
    </div>
  );
}

/** `datetime-local` value N minutes from now, in the browser's own timezone. */
function defaultLocalDateTime(minutesAhead: number): string {
  const when = new Date(Date.now() + minutesAhead * 60_000);
  const offset = when.getTimezoneOffset() * 60_000;
  return new Date(when.getTime() - offset).toISOString().slice(0, 16);
}

/** The API requires an offset; a bare `datetime-local` string carries none. */
function toIso(localValue: string): string {
  return new Date(localValue).toISOString();
}
