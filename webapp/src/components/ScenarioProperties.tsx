import clsx from "clsx";
import { Trash2 } from "lucide-react";
import type {
  AnchorMode,
  DurationMode,
  InspectBlock,
  ScenarioData,
  ScenarioElement,
  SlotKind,
} from "../api/types";
import {
  ANCHOR_LABELS,
  DURATION_LABELS,
  SLOT_LABELS,
  elementsOf,
  isRule,
  numberOf,
  trackOf,
} from "../pages/scenarioModel";

/**
 * Everything about the selected element, written as intentions rather than
 * numbers where the model has one: "five seconds before the end" is a mode
 * and an offset, not a timestamp, and that is the whole reason a scenario
 * survives meeting a clip of a different length.
 *
 * What it is *worth* on this particular clip is shown beside it, from the
 * compile, so the intention and its consequence are visible together.
 */
export default function ScenarioProperties({
  data,
  element,
  block,
  onChange,
  onRemove,
}: {
  data: ScenarioData;
  element: ScenarioElement;
  block?: InspectBlock;
  onChange: (patch: Partial<ScenarioElement>) => void;
  onRemove: () => void;
}) {
  const track = trackOf(data, element.id);
  const spine = track?.kind === "spine";

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="font-medium">{element.label || element.id}</h3>
          <p className="text-xs text-slate-500">
            {isRule(element) ? "правило" : SLOT_LABELS[element.slot?.kind ?? "source"]}
            {track && ` · ${track.id}`}
          </p>
        </div>
        <button
          className="text-slate-400 hover:text-red-600"
          onClick={onRemove}
          title="Убрать из сценария"
        >
          <Trash2 size={15} />
        </button>
      </div>

      <Field label="Подпись">
        <input
          className="input"
          value={element.label ?? ""}
          onChange={(event) => onChange({ label: event.target.value })}
        />
      </Field>

      {isRule(element) ? (
        <RuleFields element={element} onChange={onChange} />
      ) : (
        <>
          <SlotFields data={data} element={element} onChange={onChange} />
          <StartFields data={data} element={element} onChange={onChange} />
          <DurationFields element={element} onChange={onChange} />
          <FrameFields element={element} spine={spine} onChange={onChange} />
          <label className="flex items-center gap-2 text-sm text-slate-600">
            <input
              type="checkbox"
              checked={!!element.optional}
              onChange={(event) => onChange({ optional: event.target.checked })}
            />
            Необязательный — можно выбросить, если не влезает
          </label>
        </>
      )}

      {block && (
        <div
          className={clsx(
            "rounded-md border p-3 text-xs",
            block.placed
              ? "border-slate-200 bg-slate-50 text-slate-600"
              : "border-red-200 bg-red-50 text-red-700",
          )}
        >
          {block.placed ? (
            <>
              На этой длительности: {block.at_sec.toFixed(1)}–
              {(block.at_sec + block.duration_sec).toFixed(1)} с
            </>
          ) : (
            <>Не попал в клип: {block.note}</>
          )}
        </div>
      )}
    </div>
  );
}

function SlotFields({
  data,
  element,
  onChange,
}: {
  data: ScenarioData;
  element: ScenarioElement;
  onChange: (patch: Partial<ScenarioElement>) => void;
}) {
  const slot = element.slot ?? { kind: "source" as SlotKind };
  const set = (patch: Record<string, unknown>) => onChange({ slot: { ...slot, ...patch } });

  return (
    <div className="space-y-2">
      <Field label="Чем заполнено">
        <select
          className="input"
          value={slot.kind}
          onChange={(event) => set({ kind: event.target.value as SlotKind })}
        >
          {(Object.keys(SLOT_LABELS) as SlotKind[]).map((kind) => (
            <option key={kind} value={kind}>
              {SLOT_LABELS[kind]}
            </option>
          ))}
        </select>
      </Field>

      {slot.kind === "library" && (
        <Field label="Тег" hint="Что искать в библиотеке. Без тега искать нечего.">
          <input
            className="input"
            value={slot.tag ?? ""}
            onChange={(event) => set({ tag: event.target.value })}
          />
        </Field>
      )}
      {slot.kind === "color" && (
        <Field label="Цвет">
          <input
            type="color"
            className="input h-9 p-1"
            value={slot.color || "#000000"}
            onChange={(event) => set({ color: event.target.value })}
          />
        </Field>
      )}
      {slot.kind === "text" && (
        <Field label="Текст">
          <input
            className="input"
            value={slot.template ?? ""}
            onChange={(event) => set({ template: event.target.value })}
          />
        </Field>
      )}
      {slot.kind === "blur_of" && (
        <Field label="Копия чего">
          <ElementPicker
            data={data}
            exclude={element.id}
            value={slot.ref ?? ""}
            onChange={(value) => set({ ref: value })}
          />
        </Field>
      )}
    </div>
  );
}

function StartFields({
  data,
  element,
  onChange,
}: {
  data: ScenarioData;
  element: ScenarioElement;
  onChange: (patch: Partial<ScenarioElement>) => void;
}) {
  const start = element.start ?? { mode: "start" as AnchorMode };
  const set = (patch: Record<string, unknown>) => onChange({ start: { ...start, ...patch } });

  return (
    <div className="space-y-2">
      <Field label="Начало">
        <select
          className="input"
          value={start.mode}
          onChange={(event) => set({ mode: event.target.value as AnchorMode })}
        >
          {(Object.keys(ANCHOR_LABELS) as AnchorMode[]).map((mode) => (
            <option key={mode} value={mode}>
              {ANCHOR_LABELS[mode]}
            </option>
          ))}
        </select>
      </Field>

      {(start.mode === "start" || start.mode === "end") && (
        <Field
          label={start.mode === "end" ? "Сдвиг, с (минус — раньше конца)" : "Через, с"}
          hint={
            start.mode === "end"
              ? "Прижато к концу: на клипе любой длины останется там же."
              : undefined
          }
        >
          <input
            type="number"
            step="0.5"
            className="input"
            value={start.mode === "end" ? start.offset_sec ?? 0 : start.value ?? 0}
            onChange={(event) =>
              set(
                start.mode === "end"
                  ? { offset_sec: Number(event.target.value) }
                  : { value: Number(event.target.value) },
              )
            }
          />
        </Field>
      )}
      {start.mode === "fraction" && (
        <Field label="Доля клипа (0–1)">
          <input
            type="number"
            step="0.05"
            min="0"
            max="1"
            className="input"
            value={start.value ?? 0}
            onChange={(event) => set({ value: Number(event.target.value) })}
          />
        </Field>
      )}
      {(start.mode === "after" || start.mode === "before") && (
        <>
          <Field label="Относительно">
            <ElementPicker
              data={data}
              exclude={element.id}
              value={start.ref ?? ""}
              onChange={(value) => set({ ref: value })}
            />
          </Field>
          <Field label="Сдвиг, с">
            <input
              type="number"
              step="0.5"
              className="input"
              value={start.offset_sec ?? 0}
              onChange={(event) => set({ offset_sec: Number(event.target.value) })}
            />
          </Field>
        </>
      )}
    </div>
  );
}

function DurationFields({
  element,
  onChange,
}: {
  element: ScenarioElement;
  onChange: (patch: Partial<ScenarioElement>) => void;
}) {
  const duration = element.duration ?? { mode: "fixed" as DurationMode, value: 4 };
  const set = (patch: Record<string, unknown>) =>
    onChange({ duration: { ...duration, ...patch } });

  return (
    <div className="space-y-2">
      <Field label="Длительность">
        <select
          className="input"
          value={duration.mode}
          onChange={(event) => set({ mode: event.target.value as DurationMode })}
        >
          {(Object.keys(DURATION_LABELS) as DurationMode[]).map((mode) => (
            <option key={mode} value={mode}>
              {DURATION_LABELS[mode]}
            </option>
          ))}
        </select>
      </Field>
      {duration.mode === "fixed" && (
        <Field label="Секунд">
          <input
            type="number"
            step="0.5"
            min="0"
            className="input"
            value={duration.value ?? 0}
            onChange={(event) => set({ value: Number(event.target.value) })}
          />
        </Field>
      )}
      {duration.mode === "elastic" && (
        <div className="grid grid-cols-3 gap-2">
          <Field label="Доля" hint="Сколько остатка забирает относительно других эластичных.">
            <input
              type="number"
              step="0.5"
              min="0"
              className="input"
              value={duration.grow ?? 1}
              onChange={(event) => set({ grow: Number(event.target.value) })}
            />
          </Field>
          <Field label="Не меньше">
            <input
              type="number"
              step="1"
              min="0"
              className="input"
              value={duration.min_sec ?? 0}
              onChange={(event) => set({ min_sec: Number(event.target.value) })}
            />
          </Field>
          <Field label="Не больше">
            <input
              type="number"
              step="1"
              min="0"
              className="input"
              value={duration.max_sec ?? 0}
              onChange={(event) => set({ max_sec: Number(event.target.value) })}
            />
          </Field>
        </div>
      )}
    </div>
  );
}

function FrameFields({
  element,
  spine,
  onChange,
}: {
  element: ScenarioElement;
  spine: boolean;
  onChange: (patch: Partial<ScenarioElement>) => void;
}) {
  const frame = element.frame ?? {};
  const set = (patch: Record<string, unknown>) => onChange({ frame: { ...frame, ...patch } });

  return (
    <div className="space-y-2">
      <div className="grid grid-cols-4 gap-2">
        {(["x", "y", "width", "height"] as const).map((key) => (
          <Field key={key} label={key === "width" ? "ш" : key === "height" ? "в" : key}>
            <input
              type="number"
              step="1"
              className="input"
              disabled={spine}
              value={numberOf(frame[key], key === "width" || key === "height" ? 100 : 50)}
              onChange={(event) => set({ [key]: Number(event.target.value) })}
            />
          </Field>
        ))}
      </div>
      <Field label="Вписывание">
        <select
          className="input"
          value={frame.fit ?? "auto"}
          onChange={(event) => set({ fit: event.target.value })}
        >
          <option value="auto">auto — по форме исходника</option>
          <option value="cover">cover — заполнить, обрезав</option>
          <option value="contain">contain — целиком, с полями</option>
          <option value="fill">fill — растянуть</option>
        </select>
      </Field>
      {spine && (
        <p className="text-[11px] leading-snug text-amber-700">
          Позвоночник пока раскладка, а не прямоугольник: компилятор читает только
          «вписывание», а координаты отбрасывает. Двигать его здесь нечего — иначе
          редактор рисовал бы одно, а рендер делал другое.
        </p>
      )}
    </div>
  );
}

const RULE_LABELS: Record<string, string> = {
  keyword_broll: "b-roll по ключевым словам",
  on_every_cut: "звук на каждой склейке",
  cadence: "через равные промежутки",
  on_loudest: "в самых громких местах",
};

/** The two that place the rule's own template; the others have planners. */
const TEMPLATED = ["cadence", "on_loudest"];

function RuleFields({
  element,
  onChange,
}: {
  element: ScenarioElement;
  onChange: (patch: Partial<ScenarioElement>) => void;
}) {
  const rule = element.rule ?? "keyword_broll";
  const params = element.params ?? {};
  const templated = TEMPLATED.includes(rule);

  function setRule(next: string) {
    // A templated rule with no template places nothing and says so on every
    // compile. Giving it one here means choosing the rule is the whole of
    // the gesture, rather than the first half of one.
    const template =
      TEMPLATED.includes(next) && !element.template ? defaultTemplate() : element.template;
    onChange({ rule: next, template });
  }

  return (
    <div className="space-y-2">
      <Field label="Правило">
        <select className="input" value={rule} onChange={(event) => setRule(event.target.value)}>
          {Object.keys(RULE_LABELS).map((kind) => (
            <option key={kind} value={kind}>
              {RULE_LABELS[kind]}
            </option>
          ))}
        </select>
      </Field>

      {rule === "cadence" && (
        <Field label="Каждые, с">
          <input
            type="number"
            min="1"
            step="1"
            className="input"
            value={Number(params.every_sec ?? 15)}
            onChange={(event) =>
              onChange({ params: { ...params, every_sec: Number(event.target.value) } })
            }
          />
        </Field>
      )}

      <div className="grid grid-cols-2 gap-2">
        <Field label="Не больше, шт">
          <input
            type="number"
            min="0"
            className="input"
            value={element.limit ?? 4}
            onChange={(event) => onChange({ limit: Number(event.target.value) })}
          />
        </Field>
        <Field label="Зазор, с">
          <input
            type="number"
            min="0"
            step="0.5"
            className="input"
            value={element.min_gap_sec ?? 6}
            onChange={(event) => onChange({ min_gap_sec: Number(event.target.value) })}
          />
        </Field>
        <Field label="Не в первые, с" hint="Хук не трогаем.">
          <input
            type="number"
            min="0"
            step="0.5"
            className="input"
            value={element.guard_head_sec ?? 2.5}
            onChange={(event) => onChange({ guard_head_sec: Number(event.target.value) })}
          />
        </Field>
        <Field label="Не в последние, с">
          <input
            type="number"
            min="0"
            step="0.5"
            className="input"
            value={element.guard_tail_sec ?? 1.5}
            onChange={(event) => onChange({ guard_tail_sec: Number(event.target.value) })}
          />
        </Field>
      </div>

      {templated ? (
        <TemplateFields
          template={element.template ?? defaultTemplate()}
          onChange={(template) => onChange({ template })}
        />
      ) : (
        <p className="text-[11px] leading-snug text-slate-500">
          Это правило само знает, что класть: b-roll берёт фрагменты по словам, звук —
          по склейкам. Настраиваются только рамки выше.
        </p>
      )}

      <p className="text-[11px] leading-snug text-slate-500">
        Правило ничего не занимает на экране само: оно порождает элементы там, где
        совпало с материалом. На таймлайне они пунктиром, на своей дорожке.
      </p>
    </div>
  );
}

function defaultTemplate(): ScenarioElement {
  return {
    id: "шаблон",
    slot: { kind: "library", tag: "broll" },
    duration: { mode: "fixed", value: 3 },
  };
}

/** What a rule puts on screen each time it fires. */
function TemplateFields({
  template,
  onChange,
}: {
  template: ScenarioElement;
  onChange: (template: ScenarioElement) => void;
}) {
  const slot = template.slot ?? { kind: "library" as SlotKind, tag: "broll" };
  const duration = template.duration ?? { mode: "fixed" as const, value: 3 };
  const sound = !!template.audio?.enabled;

  return (
    <div className="space-y-2 rounded-md border border-emerald-200 bg-emerald-50/40 p-2">
      <p className="text-[11px] font-medium text-emerald-800">Что кладёт</p>
      <Field label="Тег в библиотеке">
        <input
          className="input"
          value={slot.tag ?? ""}
          onChange={(event) =>
            onChange({ ...template, slot: { ...slot, kind: "library", tag: event.target.value } })
          }
        />
      </Field>
      <Field label="Длительность, с">
        <input
          type="number"
          min="0"
          step="0.5"
          className="input"
          value={duration.value ?? 3}
          onChange={(event) =>
            onChange({
              ...template,
              duration: { ...duration, mode: "fixed", value: Number(event.target.value) },
            })
          }
        />
      </Field>
      <label className="flex items-center gap-2 text-sm text-slate-600">
        <input
          type="checkbox"
          checked={sound}
          onChange={(event) =>
            onChange({
              ...template,
              audio: { ...(template.audio ?? {}), enabled: event.target.checked },
            })
          }
        />
        Это звук, а не картинка
      </label>
    </div>
  );
}


function ElementPicker({
  data,
  exclude,
  value,
  onChange,
}: {
  data: ScenarioData;
  exclude: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <select className="input" value={value} onChange={(event) => onChange(event.target.value)}>
      <option value="">— не выбрано —</option>
      {elementsOf(data)
        .filter((item) => item.id !== exclude && !isRule(item))
        .map((item) => (
          <option key={item.id} value={item.id}>
            {item.label || item.id}
          </option>
        ))}
    </select>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label className="label">{label}</label>
      {children}
      {hint && <p className="mt-1 text-[11px] leading-snug text-slate-400">{hint}</p>}
    </div>
  );
}
