import { XCircle } from "lucide-react";
import { System } from "../api/client";
import { keys, useInvalidatingMutation, useTasks } from "../api/hooks";
import StatusBadge from "../components/StatusBadge";

/**
 * A window into the queue. When nothing is happening, the first question is
 * always "is a worker running, and what is it doing?" — this answers it
 * without SSH access to the container.
 */
export default function QueuePage() {
  const { data: tasks, isLoading } = useTasks();
  const cancel = useInvalidatingMutation((id: number) => System.cancelTask(id), [keys.tasks]);

  return (
    <div className="space-y-6">
      <header>
        <h2 className="text-2xl font-semibold">Queue</h2>
        <p className="text-sm text-slate-500 mt-1">
          Background work: downloading, rendering, publishing, cleanup.
        </p>
      </header>

      <div className="card p-0 overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="text-left p-3">#</th>
              <th className="text-left p-3">Kind</th>
              <th className="text-left p-3">Status</th>
              <th className="text-left p-3">Stage</th>
              <th className="text-left p-3">Attempts</th>
              <th className="p-3 text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {isLoading && (
              <tr>
                <td className="p-4 text-slate-500" colSpan={6}>
                  Loading…
                </td>
              </tr>
            )}
            {tasks?.length === 0 && (
              <tr>
                <td className="p-6 text-center text-slate-500" colSpan={6}>
                  The queue is empty.
                </td>
              </tr>
            )}
            {tasks?.map((task) => (
              <tr key={task.id}>
                <td className="p-3 text-slate-400">{task.id}</td>
                <td className="p-3 font-medium">{task.kind}</td>
                <td className="p-3">
                  <StatusBadge status={task.status} title={task.error ?? undefined} />
                </td>
                <td className="p-3">
                  <span className="text-slate-600">{task.stage}</span>
                  {task.status === "running" && (
                    <span className="text-slate-400 ml-2">
                      {Math.round(task.progress * 100)}%
                    </span>
                  )}
                  {task.error && (
                    <p className="text-xs text-red-600 max-w-md truncate" title={task.error}>
                      {task.error}
                    </p>
                  )}
                </td>
                <td className="p-3 text-slate-500">
                  {task.attempts}/{task.max_attempts}
                </td>
                <td className="p-3 text-right">
                  {(task.status === "queued" || task.status === "running") && (
                    <button
                      className="btn-secondary"
                      title="Stop this task"
                      onClick={() => cancel.mutate(task.id)}
                    >
                      <XCircle size={16} />
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
