import { useState } from "react";
import { Trash2, RefreshCcw, Download, Upload as UploadIcon, Chrome } from "lucide-react";
import { Accounts, Login, errorMessage } from "../api/client";
import { keys, useAccounts, useHealth, useInvalidatingMutation } from "../api/hooks";
import StatusBadge from "../components/StatusBadge";

export default function AccountsPage() {
  const { data: accounts, isLoading } = useAccounts();

  const importDisk = useInvalidatingMutation(Accounts.importFromDisk, [keys.accounts]);
  const resync = useInvalidatingMutation(Accounts.resync, [keys.accounts]);
  const remove = useInvalidatingMutation((id: number) => Accounts.remove(id), [keys.accounts]);

  return (
    <div className="space-y-6">
      <header className="flex items-start justify-between">
        <div>
          <h2 className="text-2xl font-semibold">Accounts</h2>
          <p className="text-sm text-slate-500 mt-1">
            One TikTok session per account, stored as a cookie file.
          </p>
        </div>
        <div className="flex gap-2">
          <button
            className="btn-secondary"
            onClick={() => resync.mutate(undefined)}
            disabled={resync.isPending}
          >
            <RefreshCcw size={16} />
            Re-check sessions
          </button>
          <button
            className="btn-secondary"
            onClick={() => importDisk.mutate(undefined)}
            disabled={importDisk.isPending}
          >
            <Download size={16} />
            Scan cookie folder
          </button>
        </div>
      </header>

      <AddAccount />

      <div className="card p-0 overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="text-left p-3">Username</th>
              <th className="text-left p-3">Session</th>
              <th className="text-left p-3">Last used</th>
              <th className="p-3 text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {isLoading && (
              <tr>
                <td className="p-4 text-slate-500" colSpan={4}>
                  Loading…
                </td>
              </tr>
            )}
            {accounts?.length === 0 && (
              <tr>
                <td className="p-6 text-slate-500 text-center" colSpan={4}>
                  No accounts yet. Add one above.
                </td>
              </tr>
            )}
            {accounts?.map((account) => (
              <tr key={account.id}>
                <td className="p-3 font-medium">{account.username}</td>
                <td className="p-3">
                  <StatusBadge
                    status={account.has_valid_session ? "ready" : "failed"}
                    title={
                      account.has_valid_session
                        ? "A session is saved for this account"
                        : "TikTok rejected this session — import cookies again"
                    }
                  />
                </td>
                <td className="p-3 text-slate-500">
                  {account.last_used_at
                    ? new Date(account.last_used_at).toLocaleString()
                    : "never"}
                </td>
                <td className="p-3 text-right">
                  <button
                    className="btn-secondary text-red-600"
                    onClick={() => {
                      if (confirm(`Delete ${account.username} and its saved session?`)) {
                        remove.mutate(account.id);
                      }
                    }}
                  >
                    <Trash2 size={16} />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/**
 * Importing a cookie file is the primary path because it works everywhere,
 * including a headless server. The browser flow needs Chrome on the machine
 * running the API, so it is the secondary option.
 */
function AddAccount() {
  const [username, setUsername] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The API says whether it could drive a browser at all. In the container it
  // cannot — the dependency is left out on purpose, and a Chrome opened inside
  // a container is one nobody can sign in to — so the button is shown as
  // unavailable rather than left to answer 503 when pressed.
  const { data: health } = useHealth();
  const browserLoginWorks = health?.browser_login ?? false;

  const importFile = useInvalidatingMutation(
    ({ username, file }: { username: string; file: File }) =>
      Login.importCookieFile(username, file),
    [keys.accounts],
  );
  const startBrowser = useInvalidatingMutation(
    (username: string) => Login.startBrowser(username),
    [keys.accounts],
  );

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    if (!username.trim() || !file) return;
    importFile.mutate(
      { username: username.trim(), file },
      {
        onSuccess: () => {
          setUsername("");
          setFile(null);
        },
        onError: (err) => setError(errorMessage(err)),
      },
    );
  };

  return (
    <form onSubmit={submit} className="card space-y-4">
      <div>
        <h3 className="font-medium">Add an account</h3>
        <p className="text-xs text-slate-500 mt-1">
          Sign in to TikTok in your browser, export its cookies with a cookie-export
          extension (JSON or cookies.txt), and upload the file here.
        </p>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="label">TikTok username</label>
          <input
            className="input"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="myaccount"
          />
        </div>
        <div>
          <label className="label">Cookie file</label>
          <input
            type="file"
            accept=".json,.txt"
            className="input py-1.5"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </div>
      </div>

      {error && <p className="text-sm text-red-600">{error}</p>}

      <div className="flex items-center gap-2">
        <button
          type="submit"
          className="btn-primary"
          disabled={!username.trim() || !file || importFile.isPending}
        >
          <UploadIcon size={16} />
          {importFile.isPending ? "Importing…" : "Import cookies"}
        </button>
        <button
          type="button"
          className="btn-secondary"
          disabled={!browserLoginWorks || !username.trim() || startBrowser.isPending}
          onClick={() =>
            startBrowser.mutate(username.trim(), {
              onError: (err) => setError(errorMessage(err)),
            })
          }
          title={
            browserLoginWorks
              ? "Opens Chrome on the machine running the API. Desktop only."
              : "This server runs without a desktop browser, so it cannot open one for you. Upload a cookie file instead."
          }
        >
          <Chrome size={16} />
          Open browser login
        </button>
        {!browserLoginWorks && (
          <span className="text-xs text-slate-500">
            Browser login needs a desktop; this server has none.
          </span>
        )}
      </div>
    </form>
  );
}
