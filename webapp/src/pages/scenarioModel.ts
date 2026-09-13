/**
 * Editing a scenario, in the shape the server stores it.
 *
 * Everything here is a pure function over the draft: nothing decides where an
 * element ends up, which is the compiler's job and nobody else's. What this
 * file knows is how to write down an intention — "this starts five seconds
 * before the end", "this is half the canvas wide" — and how to say it in
 * Russian for a panel.
 *
 * New elements are deliberately sparse. The server's decoder fills in the
 * model's own defaults for anything left out, so the editor never has to state
 * a default it did not mean to set, and there is no second copy of them here
 * to drift from the first.
 */
import type {
  AnchorMode,
  DurationMode,
  Numeric,
  ScenarioData,
  ScenarioElement,
  ScenarioTrack,
  SlotKind,
  TrackKind,
} from "../api/types";

export const SLOT_LABELS: Record<SlotKind, string> = {
  source: "Исходник",
  source_at: "Другое место исходника",
  library: "Из библиотеки",
  upload: "Загруженный файл",
  color: "Заливка",
  gradient: "Градиент",
  text: "Текст",
  blur_of: "Размытая копия",
};

/** Colour by what a block *is*, so the canvas and the timeline agree. */
export const SLOT_COLORS: Record<SlotKind, string> = {
  source: "bg-brand-500",
  source_at: "bg-brand-400",
  library: "bg-emerald-500",
  upload: "bg-teal-500",
  color: "bg-slate-500",
  gradient: "bg-slate-400",
  text: "bg-amber-500",
  blur_of: "bg-indigo-300",
};

export const ANCHOR_LABELS: Record<AnchorMode, string> = {
  start: "от начала",
  end: "от конца",
  fraction: "доля клипа",
  after: "после элемента",
  before: "перед элементом",
  event: "по событию",
};

/**
 * The mode as one glyph. An element pinned to the end is pinned to the right
 * edge, visibly — §8.2: the mistake of putting a CTA at 85 seconds and losing
 * it on a short clip should be visible with the eyes.
 */
export const ANCHOR_GLYPHS: Record<AnchorMode, string> = {
  start: "⇤",
  end: "⇥",
  fraction: "◐",
  after: "→",
  before: "←",
  event: "⚡",
};

export const DURATION_LABELS: Record<DurationMode, string> = {
  fixed: "ровно",
  elastic: "растягивается",
  rest: "всё остальное",
  until: "до якоря",
  natural: "сколько есть",
};

export const TRACK_LABELS: Record<TrackKind, string> = {
  spine: "позвоночник",
  video: "видео",
  overlay: "оверлей",
  audio: "звук",
};

export const CANVAS_WIDTH = 1080;
export const CANVAS_HEIGHT = 1920;

/** The number behind a field that may or may not be animated yet. */
export function numberOf(value: Numeric | undefined, fallback: number): number {
  if (value === undefined || value === null) return fallback;
  if (typeof value === "number") return value;
  return typeof value.static === "number" ? value.static : fallback;
}

export function isRule(element: ScenarioElement): boolean {
  return !!element.rule;
}

export function elementsOf(data: ScenarioData): ScenarioElement[] {
  return data.tracks.flatMap((track) => track.elements);
}

export function findElement(data: ScenarioData, id: string): ScenarioElement | undefined {
  return elementsOf(data).find((element) => element.id === id);
}

export function trackOf(data: ScenarioData, id: string): ScenarioTrack | undefined {
  return data.tracks.find((track) => track.elements.some((element) => element.id === id));
}

function freeId(data: ScenarioData, base: string): string {
  const taken = new Set(elementsOf(data).map((element) => element.id));
  if (!taken.has(base)) return base;
  for (let n = 2; n < 500; n += 1) {
    if (!taken.has(`${base}${n}`)) return `${base}${n}`;
  }
  return `${base}${Date.now()}`;
}

/** A scenario with nothing but the material in it. Where a new one starts. */
export function emptyScenario(name: string): ScenarioData {
  return {
    name,
    tracks: [
      {
        id: "spine",
        kind: "spine",
        elements: [
          {
            id: "source",
            label: "Исходник",
            slot: { kind: "source" },
            duration: { mode: "elastic", grow: 1 },
          },
        ],
      },
    ],
  };
}

type Addable = SlotKind | "rule" | "cadence";

/** What the palette on the left offers, and what each one starts as. */
export const PALETTE: { kind: Addable; label: string; hint: string }[] = [
  { kind: "source", label: "Кусок исходника", hint: "Ещё один отрезок материала в позвоночник" },
  { kind: "library", label: "Из библиотеки", hint: "Фрагмент по тегу — b-roll, фон, перебивка" },
  { kind: "blur_of", label: "Размытая подложка", hint: "Размытая копия того, что под ней" },
  { kind: "color", label: "Заливка", hint: "Ровный цвет — интро, аутро, подложка под текст" },
  { kind: "text", label: "Текст", hint: "Шаблон с подстановками" },
  { kind: "rule", label: "Правило", hint: "Автоматика: b-roll по ключевым словам" },
  { kind: "cadence", label: "Через промежутки", hint: "Одно и то же каждые N секунд — логотип, плашка" },
];

export function newElement(data: ScenarioData, kind: Addable): ScenarioElement {
  if (kind === "rule") {
    return {
      id: freeId(data, "broll"),
      rule: "keyword_broll",
      label: "B-roll по словам",
      limit: 3,
    };
  }
  if (kind === "cadence") {
    // The template comes with it: a rule that places nothing places nothing
    // *and says so on every compile*, which is a poor thing to hand somebody
    // who just pressed "add".
    return {
      id: freeId(data, "beat"),
      rule: "cadence",
      label: "Через промежутки",
      params: { every_sec: 20 },
      limit: 6,
      min_gap_sec: 5,
      template: {
        id: "шаблон",
        slot: { kind: "library", tag: "broll" },
        duration: { mode: "fixed", value: 3 },
      },
    };
  }
  const base: ScenarioElement = {
    id: freeId(data, kind),
    label: SLOT_LABELS[kind],
    slot: { kind },
    start: { mode: "start", value: 0 },
    duration: { mode: "fixed", value: 4 },
  };
  switch (kind) {
    case "source":
      return { ...base, duration: { mode: "elastic", grow: 1 } };
    case "library":
      // A library slot without a tag has nothing to look for, and the model
      // refuses one — so the editor never writes an element that cannot be
      // saved.
      return {
        ...base,
        slot: { kind: "library", tag: "broll" },
        frame: { x: 50, y: 50, width: 100, height: 100, fit: "cover" },
      };
    case "blur_of":
      return { ...base, slot: { kind: "blur_of", ref: "source" }, duration: { mode: "rest" } };
    case "color":
      return { ...base, slot: { kind: "color", color: "#000000" } };
    case "text":
      return { ...base, slot: { kind: "text", template: "Подпишись" }, label: "Текст" };
    default:
      return base;
  }
}

/** Which track a new element belongs on, making it if there is none. */
export function trackFor(data: ScenarioData, element: ScenarioElement): TrackKind {
  if (isRule(element)) return "overlay";
  const kind = element.slot?.kind;
  if (kind === "source" && data.tracks.every((track) => track.kind !== "spine")) return "spine";
  if (kind === "blur_of") return "video";
  return "overlay";
}

export function addElement(
  data: ScenarioData,
  element: ScenarioElement,
  kind: TrackKind,
): ScenarioData {
  const existing = data.tracks.find((track) => track.kind === kind);
  if (existing) {
    return mapTracks(data, (track) =>
      track === existing ? { ...track, elements: [...track.elements, element] } : track,
    );
  }
  const track: ScenarioTrack = {
    id: kind === "spine" ? "spine" : `${kind}${data.tracks.length + 1}`,
    kind,
    z: kind === "overlay" ? 2 : kind === "video" ? 1 : 0,
    elements: [element],
  };
  return { ...data, tracks: [...data.tracks, track] };
}

export function updateElement(
  data: ScenarioData,
  id: string,
  patch: Partial<ScenarioElement>,
): ScenarioData {
  return mapTracks(data, (track) => ({
    ...track,
    elements: track.elements.map((element) =>
      element.id === id ? { ...element, ...patch } : element,
    ),
  }));
}

export function removeElement(data: ScenarioData, id: string): ScenarioData {
  const stripped = mapTracks(data, (track) => ({
    ...track,
    elements: track.elements.filter((element) => element.id !== id),
  }));
  // A track with nothing on it is not a thing the editor can select, so it
  // would sit in the timeline as a row nobody can use. The spine stays: a
  // scenario without one is not a montage.
  return {
    ...stripped,
    tracks: stripped.tracks.filter((track) => track.kind === "spine" || track.elements.length),
  };
}

export function setTrack(data: ScenarioData, id: string, patch: Partial<ScenarioTrack>) {
  return mapTracks(data, (track) => (track.id === id ? { ...track, ...patch } : track));
}

/** Move an element's rectangle, in percent of the canvas. */
export function setFrame(
  data: ScenarioData,
  id: string,
  patch: { x?: number; y?: number; width?: number; height?: number },
): ScenarioData {
  const element = findElement(data, id);
  if (!element) return data;
  const frame = element.frame ?? {};
  return updateElement(data, id, {
    frame: {
      ...frame,
      x: patch.x !== undefined ? round(patch.x) : frame.x,
      y: patch.y !== undefined ? round(patch.y) : frame.y,
      width: patch.width !== undefined ? round(patch.width) : frame.width,
      height: patch.height !== undefined ? round(patch.height) : frame.height,
    },
  });
}

function mapTracks(data: ScenarioData, fn: (track: ScenarioTrack) => ScenarioTrack): ScenarioData {
  return { ...data, tracks: data.tracks.map(fn) };
}

function round(value: number): number {
  return Math.round(value * 10) / 10;
}
