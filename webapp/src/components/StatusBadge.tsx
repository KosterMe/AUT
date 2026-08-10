import clsx from "clsx";

// One colour vocabulary across jobs, clips, publications and tasks, so a
// yellow chip always means "working on it" wherever it appears.
const TONE: Record<string, string> = {
  // in flight
  downloading: "bg-amber-100 text-amber-800",
  transcribing: "bg-amber-100 text-amber-800",
  planning: "bg-amber-100 text-amber-800",
  rendering: "bg-amber-100 text-amber-800",
  running: "bg-amber-100 text-amber-800",
  publishing: "bg-amber-100 text-amber-800",
  // waiting
  created: "bg-slate-100 text-slate-700",
  planned: "bg-slate-100 text-slate-700",
  queued: "bg-slate-100 text-slate-700",
  scheduled: "bg-blue-100 text-blue-800",
  // done
  ready: "bg-emerald-100 text-emerald-800",
  succeeded: "bg-emerald-100 text-emerald-800",
  published: "bg-emerald-100 text-emerald-800",
  // stopped
  failed: "bg-red-100 text-red-800",
  cancelled: "bg-slate-200 text-slate-600",
};

export default function StatusBadge({ status, title }: { status: string; title?: string }) {
  return (
    <span
      title={title}
      className={clsx("chip", TONE[status] ?? "bg-slate-100 text-slate-700")}
    >
      {status}
    </span>
  );
}
