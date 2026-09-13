import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Copy,
  Film,
  Loader2,
  Plus,
  Redo2,
  Save,
  Trash2,
  Undo2,
} from "lucide-react";
import clsx from "clsx";
import { Scenarios, errorMessage } from "../api/client";
import {
  keys,
  useInvalidatingMutation,
  useJob,
  useJobs,
  useScenarioInspect,
  useScenarios,
} from "../api/hooks";
import type { Scenario, ScenarioData, ScenarioElement } from "../api/types";
import ScenarioCanvas, { type Rect } from "../components/ScenarioCanvas";
import ScenarioProperties from "../components/ScenarioProperties";
import ScenarioTimeline from "../components/ScenarioTimeline";
import {
  MOTION_PRESETS,
  PALETTE,
  addElement,
  emptyScenario,
  findElement,
  moveKey,
  newElement,
  putKey,
  removeElement,
  removeKey,
  setFrame,
  setKeyEasing,
  setKeyValue,
  setKeys,
  staticOf,
  trackFor,
  updateElement,
  type Animatable,
} from "./scenarioModel";

/**
 * The montage editor.
 *
 * The one thing that makes it an editor rather than a form: the layout
 * switcher. A scenario is written before the video exists, so the question
 * that matters is not "what does this look like" but "what does this do on a
 * clip of some other length" — and every number on this page is the answer to
 * that, compiled by the same code that renders, for the length currently
 * chosen. Nothing here works out where anything goes.
 */
export default function ScenariosPage() {
  const { data: scenarios, isLoading } = useScenarios();
  const [selectedId, setSelectedId] = useState<number | "new">("new");

  const scenario = scenarios?.find((item) => item.id === selectedId);

  return (
    <div className="space-y-5">
      <header>
        <h2 className="text-2xl font-semibold">Montages</h2>
        <p className="mt-1 text-sm text-slate-500">
          Сценарий — это монтаж, написанный до того, как появилось видео. Поэтому
          главная кнопка здесь — длительность макета: сценарий, который видели только
          на одной длине, ещё не проверяли.
        </p>
      </header>

      <div className="flex flex-wrap gap-2">
        <button
          onClick={() => setSelectedId("new")}
          className={clsx("chip border border-slate-200", selectedId === "new" && "bg-brand-50 text-brand-700")}
        >
          <Plus size={14} className="mr-1 inline" />
          Новый
        </button>
        {isLoading && <span className="text-sm text-slate-500">Loading…</span>}
        {scenarios?.map((item) => (
          <button
            key={item.id}
            onClick={() => setSelectedId(item.id)}
            className={clsx(
              "chip border border-slate-200",
              selectedId === item.id && "bg-brand-50 text-brand-700",
            )}
            title={item.description}
          >
            {item.name}
            {item.builtin && <span className="ml-1 text-slate-400">встроенный</span>}
          </button>
        ))}
      </div>

      <Editor
        key={scenario?.id ?? "new"}
        scenario={scenario}
        onSaved={(id) => setSelectedId(id)}
        onDeleted={() => setSelectedId("new")}
      />
    </div>
  );
}

function Editor({
  scenario,
  onSaved,
  onDeleted,
}: {
  scenario?: Scenario;
  onSaved: (id: number) => void;
  onDeleted: () => void;
}) {
  const saved = useMemo(
    () => scenario?.data ?? emptyScenario("Новый монтаж"),
    [scenario?.data],
  );
  const storageKey = `montage-draft:${scenario?.id ?? "new"}`;

  const [name, setName] = useState(scenario?.name ?? "Новый монтаж");
  const [description, setDescription] = useState(scenario?.description ?? "");
  const [draft, setDraft] = useState<ScenarioData>(() => restore(storageKey) ?? saved);
  const [past, setPast] = useState<ScenarioData[]>([]);
  const [future, setFuture] = useState<ScenarioData[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [at, setAt] = useState(0);
  const [duration, setDuration] = useState(90);
  const [error, setError] = useState("");
  const coalescing = useRef<{ key: string; at: number }>({ key: "", at: 0 });

  const restored = draft !== saved && !!restore(storageKey);

  // Against a closed tab, not as a save: the server still only knows what was
  // written down. §8.4.
  useEffect(() => {
    try {
      window.localStorage.setItem(storageKey, JSON.stringify(draft));
    } catch {
      // A private window, or a full quota. Losing an autosave is not worth
      // breaking the editor over.
    }
  }, [draft, storageKey]);

  /**
   * One edit. `coalesce` groups a burst of them into a single undo step —
   * dragging a block is one gesture and one undo, not two hundred.
   *
   * The burst ends when the gesture does, or after a pause. Without the
   * pause, every frame change for the rest of the session would fold into
   * the first one and undo would jump back an hour.
   */
  const apply = useCallback(
    (next: ScenarioData, coalesce = "") => {
      const now = Date.now();
      const continuing =
        !!coalesce && coalesce === coalescing.current.key && now - coalescing.current.at < 700;
      setPast((stack) => (continuing ? stack : [...stack.slice(-49), draft]));
      coalescing.current = { key: coalesce, at: now };
      setFuture([]);
      setDraft(next);
    },
    [draft],
  );

  /** A gesture finished: whatever comes next is a separate undo step. */
  const endGesture = useCallback(() => {
    coalescing.current = { key: "", at: 0 };
  }, []);

  const undo = useCallback(() => {
    setPast((stack) => {
      if (!stack.length) return stack;
      setFuture((ahead) => [draft, ...ahead]);
      setDraft(stack[stack.length - 1]);
      coalescing.current = { key: "", at: 0 };
      return stack.slice(0, -1);
    });
  }, [draft]);

  const redo = useCallback(() => {
    setFuture((ahead) => {
      if (!ahead.length) return ahead;
      setPast((stack) => [...stack, draft]);
      setDraft(ahead[0]);
      return ahead.slice(1);
    });
  }, [draft]);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== "z") return;
      const target = event.target as HTMLElement | null;
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;
      event.preventDefault();
      if (event.shiftKey) redo();
      else undo();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [undo, redo]);

  // Asked of the compiler, on the draft, at the chosen length. Debounced so a
  // held-down arrow key is one question rather than thirty.
  const asked = useDebounced(draft, 200);
  // The playhead is part of the question once anything moves: "where is this"
  // has no answer without "when".
  const askedAt = useDebounced(at, 120);
  const {
    data: report,
    error: compileError,
    isFetching,
  } = useScenarioInspect(asked, duration, askedAt);

  const save = useInvalidatingMutation(
    async () => {
      const body = { name, description, data: { ...draft, name } };
      return scenario ? Scenarios.update(scenario.id, body) : Scenarios.create(body);
    },
    [keys.scenarios],
  );

  const remove = useInvalidatingMutation(
    async () => (scenario ? Scenarios.remove(scenario.id) : undefined),
    [keys.scenarios, keys.jobs],
  );

  const element = selected ? findElement(draft, selected) : undefined;
  const block = report?.blocks.find((item) => item.element_id === selected);

  function add(kind: (typeof PALETTE)[number]["kind"]) {
    const made: ScenarioElement = newElement(draft, kind);
    apply(addElement(draft, made, trackFor(draft, made)));
    setSelected(made.id);
  }

  function moveSelected(id: string, rect: Rect) {
    apply(setFrame(draft, id, rect), `frame:${id}`);
  }

  function animate(action: "preset" | "add" | "clear", property: Animatable, name?: string) {
    if (!element) return;
    if (action === "preset") {
      const preset = MOTION_PRESETS.find((item) => item.name === name);
      if (preset) apply(preset.apply(draft, element.id));
      return;
    }
    if (action === "clear") {
      apply(setKeys(draft, element.id, property, []));
      return;
    }
    // A new key takes the value the element has right now at the playhead,
    // so pressing it twice at two moments is already a movement.
    const current = report?.blocks.find((item) => item.element_id === element.id);
    const value = current ? current.frame[property] : staticOf(element, property);
    apply(putKey(draft, element.id, property, at, value));
  }

  function editKey(
    property: Animatable,
    index: number,
    patch: { value?: number; easing?: string; remove?: boolean },
  ) {
    if (!element) return;
    if (patch.remove) {
      apply(removeKey(draft, element.id, property, index));
    } else if (patch.easing !== undefined) {
      apply(setKeyEasing(draft, element.id, property, index, patch.easing));
    } else if (patch.value !== undefined) {
      apply(
        setKeyValue(draft, element.id, property, index, patch.value),
        `key:${element.id}:${property}:${index}`,
      );
    }
  }

  return (
    <div className="space-y-4">
      <div className="card space-y-3">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-[1fr_1fr_auto]">
          <div>
            <label className="label">Название</label>
            <input
              className="input"
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </div>
          <div>
            <label className="label">Для чего</label>
            <input
              className="input"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </div>
          <div className="flex items-end gap-2">
            <button className="btn btn-secondary" onClick={undo} disabled={!past.length} title="Отменить (Ctrl+Z)">
              <Undo2 size={15} />
            </button>
            <button className="btn btn-secondary" onClick={redo} disabled={!future.length} title="Вернуть">
              <Redo2 size={15} />
            </button>
            <button
              className="btn btn-primary"
              disabled={save.isPending}
              onClick={() =>
                save
                  .mutateAsync(undefined)
                  .then((result) => {
                    setError("");
                    if (!result) return;
                    window.localStorage.removeItem(storageKey);
                    if (scenario && result.id !== scenario.id) {
                      setError(
                        `«${scenario.name}» встроенный, поэтому правка сохранена копией «${result.name}».`,
                      );
                    }
                    onSaved(result.id);
                  })
                  .catch((reason) => setError(errorMessage(reason)))
              }
            >
              {scenario?.builtin ? <Copy size={15} /> : <Save size={15} />}
              {scenario ? (scenario.builtin ? "Сохранить копией" : "Сохранить") : "Создать"}
            </button>
            {scenario && !scenario.builtin && (
              <button
                className="btn btn-danger"
                onClick={() =>
                  remove
                    .mutateAsync(undefined)
                    .then(() => {
                      window.localStorage.removeItem(storageKey);
                      onDeleted();
                    })
                    .catch((reason) => setError(errorMessage(reason)))
                }
              >
                <Trash2 size={15} />
              </button>
            )}
          </div>
        </div>
        {scenario?.builtin && (
          <p className="text-xs text-slate-500">
            Встроенный сценарий — это код, а не данные. Править можно, но сохранится
            копия: иначе одна случайная правка молча меняет все существующие джобы.
          </p>
        )}
        {restored && (
          <div className="flex items-center gap-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
            Восстановлен несохранённый черновик из этой вкладки.
            <button
              className="underline"
              onClick={() => {
                window.localStorage.removeItem(storageKey);
                apply(saved);
              }}
            >
              Вернуться к сохранённому
            </button>
          </div>
        )}
        {error && <p className="text-sm text-red-600">{error}</p>}
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[190px_1fr_330px]">
        <div className="card space-y-2">
          <h3 className="font-medium">Добавить</h3>
          {PALETTE.map((item) => (
            <button
              key={item.kind}
              onClick={() => add(item.kind)}
              className="block w-full rounded-md border border-slate-200 px-2 py-1.5 text-left
                         text-sm hover:bg-slate-50"
              title={item.hint}
            >
              {item.label}
            </button>
          ))}
        </div>

        <div className="card">
          {report ? (
            <ScenarioCanvas
              data={draft}
              blocks={report.blocks}
              at={at}
              selected={selected}
              onSelect={(id) => setSelected(id || null)}
              onMove={moveSelected}
              onMoveEnd={endGesture}
            />
          ) : (
            <p className="py-10 text-center text-sm text-slate-500">
              {compileError ? "Этот черновик не компилируется" : "Считаем…"}
            </p>
          )}
        </div>

        <div className="card">
          {element ? (
            <ScenarioProperties
              data={draft}
              element={element}
              block={block}
              at={at}
              onAnimate={animate}
              onKeyChange={editKey}
              onChange={(patch) => apply(updateElement(draft, element.id, patch), `field:${element.id}`)}
              onRemove={() => {
                apply(removeElement(draft, element.id));
                setSelected(null);
              }}
            />
          ) : (
            <p className="text-sm text-slate-500">
              Выберите блок на канвасе или на таймлайне — здесь будут его свойства.
            </p>
          )}
        </div>
      </div>

      <div className="card space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="label mb-0">Макет</span>
          {(report?.durations ?? [30, 60, 90, 120, 180]).map((length) => (
            <button
              key={length}
              onClick={() => {
                setDuration(length);
                setAt((current) => Math.min(current, length));
              }}
              className={clsx(
                "chip border border-slate-200",
                duration === length && "bg-brand-50 text-brand-700",
              )}
            >
              {length} с
            </button>
          ))}
          {isFetching && <Loader2 size={14} className="animate-spin text-slate-400" />}
          <span className="ml-auto text-xs text-slate-500">
            Переключите длительность — раскладка пересчитается компилятором, а не
            догадкой.
          </span>
        </div>

        {compileError ? (
          <p className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
            {errorMessage(compileError)}
          </p>
        ) : report ? (
          <>
            <ScenarioTimeline
              data={draft}
              report={report}
              at={at}
              onScrub={setAt}
              selected={selected}
              onSelect={(id) => setSelected(id || null)}
              onMoveKey={(property, index, atSec) =>
                element &&
                apply(
                  moveKey(draft, element.id, property, index, atSec),
                  `key:${element.id}:${property}:${index}`,
                )
              }
              onMoveKeyEnd={endGesture}
            />
            {report.warnings.length > 0 && (
              <ul className="space-y-1 text-xs text-amber-700">
                {report.warnings.map((warning, index) => (
                  <li key={index}>
                    {warning.element_id && <b>{warning.element_id}: </b>}
                    {warning.message}
                  </li>
                ))}
              </ul>
            )}
          </>
        ) : (
          <p className="text-sm text-slate-500">Считаем…</p>
        )}
      </div>

      <TryIt scenarioId={scenario?.id} draft={draft} />
    </div>
  );
}

/**
 * «Примерить»: the draft on one real clip, rendered small and fast by the same
 * ffmpeg that renders the final. The schema view above is instant and shows
 * the composition; this shows the picture, and between the two there is no
 * third answer (§8.3).
 */
function TryIt({ scenarioId, draft }: { scenarioId?: number; draft: ScenarioData }) {
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

  const [url, setUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const previous = useRef<string | null>(null);

  useEffect(() => () => {
    if (previous.current) URL.revokeObjectURL(previous.current);
  }, []);

  async function render() {
    if (!chosenClip || !scenarioId) return;
    setBusy(true);
    setError("");
    try {
      const next = await Scenarios.preview(scenarioId, {
        clip_id: chosenClip,
        data: draft,
        duration_sec: 4,
      });
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
    <div className="card space-y-3">
      <div>
        <h3 className="font-medium">Примерить</h3>
        <p className="mt-1 text-xs leading-snug text-slate-500">
          Четыре секунды настоящего клипа, собранные этим сценарием — несохранённым
          тоже. Тот же код, что и финальный рендер, только меньше и быстрее.
        </p>
      </div>

      {!scenarioId ? (
        <p className="text-sm text-slate-500">Сначала сохраните сценарий.</p>
      ) : candidates.length === 0 ? (
        <p className="text-sm text-slate-500">
          Нет ни одного отрендеренного клипа, на котором это можно показать.
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
          <button className="btn btn-secondary" onClick={render} disabled={busy || !chosenClip}>
            {busy ? <Loader2 size={15} className="animate-spin" /> : <Film size={15} />}
            Примерить
          </button>
          {error && <p className="text-sm text-red-600">{error}</p>}
          {url && <video src={url} controls className="w-full max-w-[240px] rounded-md" />}
        </>
      )}
    </div>
  );
}

/** The value, but only after it has stopped moving. */
function useDebounced<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), ms);
    return () => window.clearTimeout(timer);
  }, [value, ms]);
  return settled;
}

function restore(key: string): ScenarioData | null {
  try {
    const stored = window.localStorage.getItem(key);
    return stored ? (JSON.parse(stored) as ScenarioData) : null;
  } catch {
    return null;
  }
}
