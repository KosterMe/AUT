import { NavLink } from "react-router-dom";
import { Users, Calendar, Scissors, ListChecks, Film, Layers, Palette } from "lucide-react";
import clsx from "clsx";
import { useHealth } from "../api/hooks";

const items = [
  { to: "/jobs", label: "Videos", icon: Scissors },
  { to: "/library", label: "Library", icon: Film },
  { to: "/looks", label: "Looks", icon: Palette },
  { to: "/montages", label: "Montages", icon: Layers },
  { to: "/publications", label: "Schedule", icon: Calendar },
  { to: "/accounts", label: "Accounts", icon: Users },
  { to: "/queue", label: "Queue", icon: ListChecks },
];

export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen flex bg-slate-50">
      <aside className="w-56 shrink-0 border-r border-slate-200 bg-white flex flex-col">
        <div className="p-5 border-b border-slate-200">
          <h1 className="text-lg font-semibold tracking-tight">TikTok Poster</h1>
          <p className="text-xs text-slate-500 mt-1">Clip, schedule, publish</p>
        </div>
        <nav className="p-3 space-y-1 flex-1">
          {items.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                clsx(
                  "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                  isActive
                    ? "bg-brand-50 text-brand-700"
                    : "text-slate-600 hover:bg-slate-50 hover:text-slate-900",
                )
              }
            >
              <Icon size={18} />
              {label}
            </NavLink>
          ))}
        </nav>
        <WorkerStatus />
      </aside>
      <main className="flex-1 p-8 max-w-6xl">{children}</main>
    </div>
  );
}

/**
 * Whether background work is actually moving. Without this, a stopped worker
 * looks exactly like a slow one: jobs simply sit there with no explanation.
 */
function WorkerStatus() {
  const { data, isError } = useHealth();

  if (isError) {
    return (
      <div className="border-t border-slate-200 p-4 text-xs text-red-600">
        API unreachable
      </div>
    );
  }
  if (!data) return null;

  const running = data.tasks.running ?? 0;
  const queued = data.tasks.queued ?? 0;
  // Tasks past their run time with nothing running means no worker is serving them.
  const stalled = data.tasks_due_now > 0 && running === 0;

  return (
    <div className="border-t border-slate-200 p-4 text-xs space-y-1">
      <div className="flex items-center gap-2">
        <span
          className={clsx(
            "h-2 w-2 rounded-full",
            stalled ? "bg-red-500" : running > 0 ? "bg-amber-500" : "bg-emerald-500",
          )}
        />
        <span className="text-slate-600">
          {running > 0 ? `${running} running` : "idle"}
          {queued > 0 && `, ${queued} queued`}
        </span>
      </div>
      {stalled && (
        <p className="text-red-600 leading-snug">
          {data.tasks_due_now} task(s) are due but nothing is running — is a worker started?
        </p>
      )}
      <p className="text-slate-400">v{data.version}</p>
    </div>
  );
}
