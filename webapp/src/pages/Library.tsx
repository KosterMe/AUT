import { useRef, useState } from "react";
import { Film, Image as ImageIcon, Music, Trash2, Upload as UploadIcon } from "lucide-react";
import { Assets, errorMessage } from "../api/client";
import { keys, useAssets, useInvalidatingMutation } from "../api/hooks";
import type { MediaAsset } from "../api/types";

export default function LibraryPage() {
  const { data: assets, isLoading } = useAssets();

  return (
    <div className="space-y-6">
      <header>
        <h2 className="text-2xl font-semibold">Library</h2>
        <p className="text-sm text-slate-500 mt-1">
          Everything laid over clips automatically. Tags are what places it: an asset
          tagged <span className="font-medium">машина</span> goes on screen the moment
          that word is spoken.
        </p>
        <p className="text-sm text-slate-500 mt-1">
          Three tags mean something on their own —{" "}
          <span className="font-medium">music</span> is a bed that ducks under speech,{" "}
          <span className="font-medium">sfx</span> is a sound played on cuts, and{" "}
          <span className="font-medium">background</span> fills the bottom half of a
          split screen. Each answers to its Russian name too:{" "}
          <span className="font-medium">музыка</span>,{" "}
          <span className="font-medium">звук</span>,{" "}
          <span className="font-medium">фон</span>.
        </p>
      </header>

      <UploadAssets />

      {isLoading && <p className="text-sm text-slate-500">Loading…</p>}

      {assets?.length === 0 && (
        <div className="card text-center text-slate-500 py-10">
          Nothing here yet. Clips render without b-roll, music or effects until you
          add something.
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4">
        {assets?.map((asset) => (
          <AssetCard key={asset.id} asset={asset} />
        ))}
      </div>
    </div>
  );
}

/** What went wrong with one file of a batch, so the rest can still go in. */
interface FailedUpload {
  name: string;
  reason: string;
}

function UploadAssets() {
  const [tags, setTags] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [done, setDone] = useState(0);
  const [failed, setFailed] = useState<FailedUpload[]>([]);
  const input = useRef<HTMLInputElement>(null);

  const upload = useInvalidatingMutation(
    async ({ files, tags }: { files: File[]; tags: string }) => {
      // One request per file, and one rejection does not take the batch with
      // it: picking ten clips of which one was a .wmv used to upload nothing
      // after that .wmv, with no indication of which ones had made it.
      const rejected: FailedUpload[] = [];
      for (const file of files) {
        try {
          await Assets.upload(file, tags);
        } catch (error) {
          rejected.push({ name: file.name, reason: errorMessage(error) });
        }
        setDone((count) => count + 1);
      }
      return rejected;
    },
    [keys.assets],
  );

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!files.length) return;
    setFailed([]);
    setDone(0);
    upload.mutate(
      { files, tags },
      {
        onSuccess: (rejected) => {
          setFailed(rejected);
          setFiles([]);
          // Keep the tags when something was refused: they were typed for the
          // files that now have to be picked again.
          if (!rejected.length) setTags("");
          if (input.current) input.current.value = "";
        },
      },
    );
  };

  return (
    <form className="card space-y-4" onSubmit={submit}>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="label">Files</label>
          <input
            ref={input}
            className="input"
            type="file"
            multiple
            accept="video/*,image/*,audio/*"
            onChange={(event) => setFiles(Array.from(event.target.files ?? []))}
          />
          <p className="text-xs text-slate-400 mt-1">
            Video plays for a few seconds; an image is held on screen.
          </p>
        </div>
        <div>
          <label className="label">Tags</label>
          <input
            className="input"
            value={tags}
            onChange={(event) => setTags(event.target.value)}
            placeholder="машина, дорога"
          />
          <p className="text-xs text-slate-400 mt-1">
            Words the clip might say. Tags are the only thing that puts an asset on
            screen — one that matches nothing is never placed.
          </p>
        </div>
      </div>

      {failed.length > 0 && (
        <div className="text-sm text-red-600 space-y-1">
          {failed.map((item) => (
            <p key={item.name}>
              <span className="font-medium">{item.name}</span>: {item.reason}
            </p>
          ))}
        </div>
      )}

      <div className="flex items-center justify-end gap-3">
        {upload.isPending && files.length > 1 && (
          <span className="text-xs text-slate-500">
            {done} / {files.length}
          </span>
        )}
        <button className="btn-primary" disabled={!files.length || upload.isPending}>
          <UploadIcon size={16} />
          {upload.isPending ? "Uploading…" : `Add ${files.length || ""}`.trim()}
        </button>
      </div>
    </form>
  );
}

function AssetCard({ asset }: { asset: MediaAsset }) {
  const stored = asset.tags.join(", ");
  const [tags, setTags] = useState(stored);
  // The field follows what was actually saved, and the save is what says so —
  // tags come back normalised (lowercased, deduplicated, hashes stripped), and
  // normalising "Машина" to "машина" leaves the list unchanged, so waiting for
  // the refetched asset to look different never resyncs this at all. Left
  // alone, the box stayed permanently out of step and re-saved on every blur.
  const [lastStored, setLastStored] = useState(stored);
  if (stored !== lastStored) {
    setLastStored(stored);
    setTags(stored);
  }

  const isAudio = asset.kind === "audio";
  // A .gif is stored as a video, because ffmpeg treats it as one — but no
  // browser plays one in a <video>.
  const isStill = asset.kind === "image" || /\.gif$/i.test(asset.original_name);

  const save = useInvalidatingMutation(
    (value: string) => Assets.setTags(asset.id, value),
    [keys.assets],
  );
  const remove = useInvalidatingMutation(() => Assets.remove(asset.id), [keys.assets]);
  const problem = save.error ?? remove.error;

  return (
    <div className="card space-y-3">
      <div className="aspect-[9/16] max-h-56 rounded-md bg-slate-100 overflow-hidden flex items-center justify-center">
        {isAudio ? (
          <div className="flex flex-col items-center gap-3 px-4 w-full">
            <Music size={28} className="text-slate-400" />
            <audio src={Assets.fileUrl(asset.id)} controls preload="none" className="w-full" />
          </div>
        ) : isStill ? (
          <img
            src={Assets.fileUrl(asset.id)}
            alt=""
            loading="lazy"
            className="h-full w-full object-cover"
          />
        ) : (
          // preload="metadata" plus a #t=0.1 seek: the card shows its first
          // frame after a few kilobytes. Without it, opening this page
          // downloaded every video in the library in full, every time.
          <video
            src={`${Assets.fileUrl(asset.id)}#t=0.1`}
            className="h-full w-full object-cover"
            muted
            controls
            preload="metadata"
          />
        )}
      </div>

      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-sm font-medium truncate" title={asset.original_name}>
            {asset.original_name}
          </p>
          <p className="text-xs text-slate-400 flex items-center gap-1">
            {isAudio ? <Music size={12} /> : isStill ? <ImageIcon size={12} /> : <Film size={12} />}
            {asset.duration_sec ? `${asset.duration_sec.toFixed(1)}s · ` : ""}
            {(asset.size_bytes / 1e6).toFixed(1)} MB
          </p>
        </div>
        <button
          type="button"
          className="btn-secondary text-red-600 shrink-0"
          title="Delete this asset"
          disabled={remove.isPending}
          onClick={() => {
            if (confirm(`Delete ${asset.original_name}?`)) remove.mutate(undefined);
          }}
        >
          <Trash2 size={16} />
        </button>
      </div>

      <div>
        <input
          className="input text-xs"
          value={tags}
          onChange={(event) => setTags(event.target.value)}
          onBlur={() => {
            if (tags === stored) return;
            save.mutate(tags, {
              onSuccess: (updated) => {
                const normalised = updated.tags.join(", ");
                setTags(normalised);
                setLastStored(normalised);
              },
            });
          }}
          placeholder="no tags"
        />
        {problem ? (
          <p className="text-xs text-red-600 mt-1">{errorMessage(problem)}</p>
        ) : (
          <p className="text-xs text-slate-400 mt-1">
            {asset.use_count > 0 && asset.last_used_at
              ? `used ${asset.use_count}×, last ${new Date(asset.last_used_at).toLocaleDateString()}`
              : "never used yet"}
          </p>
        )}
      </div>
    </div>
  );
}
