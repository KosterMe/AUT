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
          split screen.
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

function UploadAssets() {
  const [tags, setTags] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);

  const upload = useInvalidatingMutation(
    async ({ files, tags }: { files: File[]; tags: string }) => {
      // One request per file so a single rejected format does not lose the rest.
      for (const file of files) {
        await Assets.upload(file, tags);
      }
    },
    [keys.assets],
  );

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    if (!files.length) return;
    upload.mutate(
      { files, tags },
      {
        onSuccess: () => {
          setFiles([]);
          setTags("");
          if (input.current) input.current.value = "";
        },
        onError: (err) => setError(errorMessage(err)),
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
            Words the clip might say. Untagged assets are only used to fill a fixed beat.
          </p>
        </div>
      </div>

      {error && <p className="text-sm text-red-600">{error}</p>}

      <div className="flex justify-end">
        <button className="btn-primary" disabled={!files.length || upload.isPending}>
          <UploadIcon size={16} />
          {upload.isPending ? "Uploading…" : `Add ${files.length || ""}`.trim()}
        </button>
      </div>
    </form>
  );
}

function AssetCard({ asset }: { asset: MediaAsset }) {
  const [tags, setTags] = useState(asset.tags.join(", "));
  const isImage = asset.kind === "image";
  const isAudio = asset.kind === "audio";

  const save = useInvalidatingMutation(
    (value: string) => Assets.setTags(asset.id, value),
    [keys.assets],
  );
  const remove = useInvalidatingMutation(() => Assets.remove(asset.id), [keys.assets]);

  const dirty = tags !== asset.tags.join(", ");

  return (
    <div className="card space-y-3">
      <div className="aspect-[9/16] max-h-56 rounded-md bg-slate-100 overflow-hidden flex items-center justify-center">
        {isAudio ? (
          <div className="flex flex-col items-center gap-3 px-4 w-full">
            <Music size={28} className="text-slate-400" />
            <audio src={Assets.fileUrl(asset.id)} controls className="w-full" />
          </div>
        ) : isImage ? (
          <img src={Assets.fileUrl(asset.id)} alt="" className="h-full w-full object-cover" />
        ) : (
          <video src={Assets.fileUrl(asset.id)} className="h-full w-full object-cover" muted controls />
        )}
      </div>

      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-sm font-medium truncate" title={asset.original_name}>
            {asset.original_name}
          </p>
          <p className="text-xs text-slate-400 flex items-center gap-1">
            {isAudio ? <Music size={12} /> : isImage ? <ImageIcon size={12} /> : <Film size={12} />}
            {asset.duration_sec ? `${asset.duration_sec.toFixed(1)}s · ` : ""}
            {(asset.size_bytes / 1e6).toFixed(1)} MB
          </p>
        </div>
        <button
          className="btn-secondary text-red-600 shrink-0"
          title="Delete this asset"
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
          onBlur={() => dirty && save.mutate(tags)}
          placeholder="no tags"
        />
        <p className="text-xs text-slate-400 mt-1">
          {asset.use_count > 0
            ? `used ${asset.use_count}×, last ${new Date(asset.last_used_at!).toLocaleDateString()}`
            : "never used yet"}
        </p>
      </div>
    </div>
  );
}
