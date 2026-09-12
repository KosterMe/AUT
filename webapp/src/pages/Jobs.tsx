import { useState } from "react";
import { Link } from "react-router-dom";
import { Plus, Scissors } from "lucide-react";
import { Jobs, errorMessage } from "../api/client";
import { keys, useInvalidatingMutation, useJobs, useStyles } from "../api/hooks";
import StatusBadge from "../components/StatusBadge";
import { JOB_PROFILES } from "../api/types";
import type { ClipJob, JobProfile } from "../api/types";

export default function JobsPage() {
  const { data: jobs, isLoading } = useJobs();

  return (
    <div className="space-y-6">
      <header>
        <h2 className="text-2xl font-semibold">Videos</h2>
        <p className="text-sm text-slate-500 mt-1">
          Add a source video; it gets downloaded, transcribed and cut into vertical clips.
        </p>
      </header>

      <NewJobForm />

      <div className="space-y-3">
        {isLoading && <p className="text-sm text-slate-500">Loading…</p>}
        {jobs?.length === 0 && (
          <div className="card text-center text-slate-500 py-10">
            <Scissors className="mx-auto mb-2 text-slate-300" size={28} />
            No videos yet.
          </div>
        )}
        {jobs?.map((job) => (
          <JobRow key={job.id} job={job} />
        ))}
      </div>
    </div>
  );
}

function JobRow({ job }: { job: ClipJob }) {
  const title = job.custom_title || job.title || job.source_ref;
  const busy = !["ready", "failed", "cancelled"].includes(job.status);

  return (
    <Link to={`/jobs/${job.id}`} className="card block hover:border-brand-300 transition-colors">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h3 className="font-medium truncate">{title}</h3>
            <StatusBadge status={job.status} title={job.error ?? undefined} />
            <span
              className="text-xs text-slate-500 border border-slate-200 rounded px-1.5 py-0.5 shrink-0"
              title={JOB_PROFILES.find((option) => option.id === job.profile)?.hint}
            >
              {JOB_PROFILES.find((option) => option.id === job.profile)?.name ?? job.profile}
            </span>
          </div>
          <p className="text-xs text-slate-500 mt-1 truncate">{job.source_ref}</p>
          {job.error && <p className="text-xs text-red-600 mt-1">{job.error}</p>}
        </div>
        <div className="text-right text-xs text-slate-500 shrink-0">
          {job.duration_seconds ? formatDuration(job.duration_seconds) : "—"}
          <div className="mt-1">{new Date(job.created_at).toLocaleDateString()}</div>
        </div>
      </div>

      {busy && (
        <div className="mt-3">
          <div className="flex items-center justify-between text-xs text-slate-500 mb-1">
            <span>{job.stage ?? job.status}</span>
            <span>{Math.round(job.progress * 100)}%</span>
          </div>
          <div className="h-1.5 rounded-full bg-slate-100 overflow-hidden">
            <div
              className="h-full bg-brand-500 transition-all"
              style={{ width: `${Math.max(3, job.progress * 100)}%` }}
            />
          </div>
        </div>
      )}
    </Link>
  );
}

function NewJobForm() {
  const [open, setOpen] = useState(false);
  const [sourceRef, setSourceRef] = useState("");
  const [profile, setProfile] = useState<JobProfile>("talking");
  const [styleId, setStyleId] = useState("");
  const [customTitle, setCustomTitle] = useState("");
  const [captionTags, setCaptionTags] = useState("");
  const [minSeconds, setMinSeconds] = useState(90);
  const [maxSeconds, setMaxSeconds] = useState(120);
  const [maxClips, setMaxClips] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const { data: styles } = useStyles();
  const create = useInvalidatingMutation(Jobs.create, [keys.jobs]);

  if (!open) {
    return (
      <button className="btn-primary" onClick={() => setOpen(true)}>
        <Plus size={16} />
        Add a video
      </button>
    );
  }

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    create.mutate(
      {
        source_ref: sourceRef.trim(),
        profile,
        style_id: styleId ? Number(styleId) : null,
        custom_title: customTitle.trim() || null,
        caption_tags: captionTags.trim() || null,
        min_clip_seconds: minSeconds,
        max_clip_seconds: maxSeconds,
        max_clips: maxClips,
        start_immediately: true,
      },
      {
        onSuccess: () => {
          setSourceRef("");
          setCustomTitle("");
          setCaptionTags("");
          setOpen(false);
        },
        onError: (err) => setError(errorMessage(err)),
      },
    );
  };

  return (
    <form onSubmit={submit} className="card space-y-4">
      <div>
        <label className="label">YouTube URL or a path to a local file</label>
        <input
          className="input"
          value={sourceRef}
          onChange={(e) => setSourceRef(e.target.value)}
          placeholder="https://www.youtube.com/watch?v=…"
          autoFocus
        />
      </div>

      <div>
        <label className="label">What kind of video is this?</label>
        <div className="grid grid-cols-4 gap-2">
          {JOB_PROFILES.map((option) => (
            <button
              key={option.id}
              type="button"
              onClick={() => setProfile(option.id)}
              className={`rounded-lg border px-3 py-2 text-left text-sm transition-colors ${
                profile === option.id
                  ? "border-brand-500 bg-brand-50 text-brand-700"
                  : "border-slate-200 hover:border-slate-300"
              }`}
            >
              {option.name}
            </button>
          ))}
        </div>
        <p className="text-xs text-slate-400 mt-1">
          {JOB_PROFILES.find((option) => option.id === profile)?.hint}
        </p>
      </div>

      <div>
        <label className="label">Look (optional)</label>
        <select className="input" value={styleId} onChange={(e) => setStyleId(e.target.value)}>
          <option value="">The profile's own defaults</option>
          {styles?.map((preset) => (
            <option key={preset.id} value={preset.id}>
              {preset.name}
            </option>
          ))}
        </select>
        <p className="text-xs text-slate-400 mt-1">
          A saved look overrides the parts of the profile it has an opinion about.{" "}
          <Link to="/looks" className="text-brand-600 hover:underline">
            Edit looks
          </Link>
        </p>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="label">Video name (optional)</label>
          <input
            className="input"
            value={customTitle}
            onChange={(e) => setCustomTitle(e.target.value)}
            placeholder="Left blank: taken from the source"
          />
          <p className="text-xs text-slate-400 mt-1">
            Burned on each clip and used as the caption's first line.
          </p>
        </div>
        <div>
          <label className="label">Hashtags (optional)</label>
          <input
            className="input"
            value={captionTags}
            onChange={(e) => setCaptionTags(e.target.value)}
            placeholder="#tag @mention"
          />
          <p className="text-xs text-slate-400 mt-1">
            When set, these replace the auto-detected tags entirely.
          </p>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-4">
        <div>
          <label className="label">Min clip (sec)</label>
          <input
            type="number"
            className="input"
            value={minSeconds}
            min={5}
            max={180}
            onChange={(e) => setMinSeconds(Number(e.target.value))}
          />
        </div>
        <div>
          <label className="label">Max clip (sec)</label>
          <input
            type="number"
            className="input"
            value={maxSeconds}
            min={10}
            max={300}
            onChange={(e) => setMaxSeconds(Number(e.target.value))}
          />
        </div>
        <div>
          <label className="label">Max clips (0 = all)</label>
          <input
            type="number"
            className="input"
            value={maxClips}
            min={0}
            max={500}
            onChange={(e) => setMaxClips(Number(e.target.value))}
          />
        </div>
      </div>

      <p className="text-xs text-slate-400">
        Clips end on sentence boundaries, so the length is a target rather than an exact cut.
      </p>

      {error && <p className="text-sm text-red-600">{error}</p>}

      <div className="flex gap-2">
        <button
          type="submit"
          className="btn-primary"
          disabled={!sourceRef.trim() || create.isPending}
        >
          {create.isPending ? "Starting…" : "Start"}
        </button>
        <button type="button" className="btn-secondary" onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
    </form>
  );
}

export function formatDuration(seconds: number): string {
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (value: number) => String(value).padStart(2, "0");
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(secs)}` : `${minutes}:${pad(secs)}`;
}
