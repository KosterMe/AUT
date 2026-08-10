import { useState } from "react";
import { Link } from "react-router-dom";
import { RotateCcw, Trash2, XCircle } from "lucide-react";
import { Publications, errorMessage } from "../api/client";
import { keys, useAccounts, useInvalidatingMutation, usePublications } from "../api/hooks";
import StatusBadge from "../components/StatusBadge";
import type { Publication, PublicationStatus } from "../api/types";

const FILTERS: { label: string; value?: PublicationStatus }[] = [
  { label: "All" },
  { label: "Scheduled", value: "scheduled" },
  { label: "Published", value: "published" },
  { label: "Failed", value: "failed" },
];

export default function PublicationsPage() {
  const [filter, setFilter] = useState<PublicationStatus | undefined>(undefined);
  const { data: publications, isLoading } = usePublications(filter);
  const { data: accounts } = useAccounts();

  const cancelAll = useInvalidatingMutation(
    () => Publications.cancelScheduled(),
    [keys.publications(), keys.publications("scheduled")],
  );

  const accountName = (id: number) =>
    accounts?.find((a) => a.id === id)?.username ?? `#${id}`;

  return (
    <div className="space-y-6">
      <header className="flex items-start justify-between">
        <div>
          <h2 className="text-2xl font-semibold">Schedule</h2>
          <p className="text-sm text-slate-500 mt-1">
            Every post, waiting or already out.
          </p>
        </div>
        <button
          className="btn-secondary"
          onClick={() => {
            if (confirm("Cancel every post that has not gone out yet?")) {
              cancelAll.mutate(undefined);
            }
          }}
        >
          <XCircle size={16} />
          Cancel all pending
        </button>
      </header>

      <div className="flex gap-2">
        {FILTERS.map((item) => (
          <button
            key={item.label}
            className={filter === item.value ? "btn-primary" : "btn-secondary"}
            onClick={() => setFilter(item.value)}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div className="card p-0 overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="text-left p-3">When</th>
              <th className="text-left p-3">Account</th>
              <th className="text-left p-3">Caption</th>
              <th className="text-left p-3">Status</th>
              <th className="p-3 text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {isLoading && (
              <tr>
                <td className="p-4 text-slate-500" colSpan={5}>
                  Loading…
                </td>
              </tr>
            )}
            {publications?.length === 0 && (
              <tr>
                <td className="p-6 text-center text-slate-500" colSpan={5}>
                  Nothing here. Schedule clips from a{" "}
                  <Link to="/jobs" className="text-brand-600 underline">
                    video
                  </Link>
                  .
                </td>
              </tr>
            )}
            {publications?.map((row) => (
              <Row key={row.id} row={row} accountName={accountName(row.account_id)} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Row({ row, accountName }: { row: Publication; accountName: string }) {
  const [error, setError] = useState<string | null>(null);
  const invalidate = [keys.publications(), keys.publications(row.status), keys.jobs];

  const cancel = useInvalidatingMutation(() => Publications.cancel(row.id), invalidate);
  const retry = useInvalidatingMutation(() => Publications.retry(row.id), invalidate);
  const remove = useInvalidatingMutation(() => Publications.remove(row.id), invalidate);

  const onError = (err: unknown) => setError(errorMessage(err));

  return (
    <tr>
      <td className="p-3 whitespace-nowrap">
        {new Date(row.scheduled_at).toLocaleString()}
      </td>
      <td className="p-3">{accountName}</td>
      <td className="p-3 max-w-md">
        <p className="truncate" title={row.caption}>
          {row.caption.split("\n")[0]}
        </p>
        {row.result_text && (
          <p className="text-xs text-slate-400 truncate" title={row.result_text}>
            {row.result_text}
          </p>
        )}
        {error && <p className="text-xs text-red-600">{error}</p>}
      </td>
      <td className="p-3">
        <StatusBadge status={row.status} />
      </td>
      <td className="p-3 text-right whitespace-nowrap space-x-1">
        {row.status === "scheduled" && (
          <button
            className="btn-secondary"
            title="Cancel this post"
            onClick={() => cancel.mutate(undefined, { onError })}
          >
            <XCircle size={16} />
          </button>
        )}
        {row.status === "failed" && (
          <button
            className="btn-secondary"
            title="Re-queue. Check the account first — a failed upload may still have gone live."
            onClick={() => retry.mutate(undefined, { onError })}
          >
            <RotateCcw size={16} />
          </button>
        )}
        {row.status !== "publishing" && (
          <button
            className="btn-secondary text-red-600"
            title="Remove from the list"
            onClick={() => remove.mutate(undefined, { onError })}
          >
            <Trash2 size={16} />
          </button>
        )}
      </td>
    </tr>
  );
}
