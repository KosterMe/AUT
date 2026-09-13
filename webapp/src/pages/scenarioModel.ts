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
  ScenarioKeyframe,
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


// --- animation ---------------------------------------------------------------
//
// A keyframe is anchored rather than timed, for the same reason everything
// else in a scenario is: "leave half a second before the end" has to keep
// meaning that on a clip of another length. The editor writes `start`-anchored
// keys by default and drags those; a key anchored to the end is shown and
// respected but dragged by its offset, because that is the number it has.

export const ANIMATABLE = ["x", "y"] as const;
export type Animatable = (typeof ANIMATABLE)[number];

export const PROPERTY_LABELS: Record<Animatable, string> = { x: "по горизонтали", y: "по вертикали" };

export function keysOf(element: ScenarioElement, property: Animatable): ScenarioKeyframe[] {
  const value = element.frame?.[property];
  if (!value || typeof value === "number") return [];
  return value.keys ?? [];
}

export function staticOf(element: ScenarioElement, property: Animatable): number {
  return numberOf(element.frame?.[property], 50);
}

export function setKeys(
  data: ScenarioData,
  id: string,
  property: Animatable,
  keys: ScenarioKeyframe[],
): ScenarioData {
  const element = findElement(data, id);
  if (!element) return data;
  const frame = element.frame ?? {};
  const still = staticOf(element, property);
  return updateElement(data, id, {
    frame: {
      ...frame,
      // The static value stays what it was: it is what the element is worth
      // when the keys are taken away again, and `is_static` is the cheap path
      // a scenario without animation must keep (§4.3).
      [property]: keys.length ? { static: still, keys } : still,
    },
  });
}

/** A key at that second, replacing one already there. */
export function putKey(
  data: ScenarioData,
  id: string,
  property: Animatable,
  atSec: number,
  value: number,
): ScenarioData {
  const element = findElement(data, id);
  if (!element) return data;
  const kept = keysOf(element, property).filter(
    (key) => !(key.at.mode === "start" && Math.abs((key.at.value ?? 0) - atSec) < 0.05),
  );
  const next: ScenarioKeyframe = {
    at: { mode: "start", value: round1(atSec) },
    value: round1(value),
    easing: "linear",
  };
  return setKeys(data, id, property, [...kept, next].sort(byTime));
}

export function moveKey(
  data: ScenarioData,
  id: string,
  property: Animatable,
  index: number,
  atSec: number,
): ScenarioData {
  const element = findElement(data, id);
  if (!element) return data;
  const keys = keysOf(element, property).map((key, position) => {
    if (position !== index) return key;
    // An end-anchored key has no absolute second of its own: what moves is
    // how far before the end it sits.
    if (key.at.mode === "end") {
      return { ...key, at: { ...key.at, offset_sec: round1(atSec) } };
    }
    return { ...key, at: { ...key.at, mode: "start" as const, value: round1(Math.max(0, atSec)) } };
  });
  return setKeys(data, id, property, keys);
}

export function setKeyValue(
  data: ScenarioData,
  id: string,
  property: Animatable,
  index: number,
  value: number,
): ScenarioData {
  const element = findElement(data, id);
  if (!element) return data;
  return setKeys(
    data, id, property,
    keysOf(element, property).map(
      (key, position) => (position === index ? { ...key, value: round1(value) } : key),
    ),
  );
}

export function setKeyEasing(
  data: ScenarioData,
  id: string,
  property: Animatable,
  index: number,
  easing: string,
): ScenarioData {
  const element = findElement(data, id);
  if (!element) return data;
  return setKeys(
    data, id, property,
    keysOf(element, property).map(
      (key, position) => (position === index ? { ...key, easing } : key),
    ),
  );
}

export function removeKey(
  data: ScenarioData,
  id: string,
  property: Animatable,
  index: number,
): ScenarioData {
  const element = findElement(data, id);
  if (!element) return data;
  return setKeys(
    data, id, property, keysOf(element, property).filter((_, position) => position !== index),
  );
}

/**
 * Ready-made sets of keyframes. Not a separate kind of thing — §4.3 is
 * explicit that a preset *is* keyframes — so applying one leaves an element
 * anybody can then drag.
 *
 * Only position, because only position animates on this build without costing
 * something absurd: scale wants `zoompan`, an arbitrary opacity curve is `geq`
 * at ×23, rotation is ×3.3 (§7.2).
 */
export const MOTION_PRESETS: {
  name: string;
  hint: string;
  apply: (data: ScenarioData, id: string) => ScenarioData;
}[] = [
  {
    name: "Въезд слева",
    hint: "За полсекунды из-за левого края на своё место",
    apply: (data, id) => fromOffscreen(data, id, "x", -20),
  },
  {
    name: "Въезд снизу",
    hint: "За полсекунды снизу на своё место",
    apply: (data, id) => fromOffscreen(data, id, "y", 120),
  },
  {
    name: "Уезд вправо",
    hint: "Уходит за правый край в конце, прижато к концу клипа",
    apply: (data, id) => offscreenAtEnd(data, id, "x", 120),
  },
  {
    name: "Проезд насквозь",
    hint: "Слева направо через весь кадр, пока элемент на экране",
    apply: (data, id) =>
      setKeys(data, id, "x", [
        { at: { mode: "start", value: 0 }, value: -20, easing: "linear" },
        { at: { mode: "end", offset_sec: 0 }, value: 120, easing: "linear" },
      ]),
  },
];

function fromOffscreen(
  data: ScenarioData,
  id: string,
  property: Animatable,
  from: number,
): ScenarioData {
  const element = findElement(data, id);
  if (!element) return data;
  const home = staticOf(element, property);
  return setKeys(data, id, property, [
    { at: { mode: "start", value: 0 }, value: from, easing: "out" },
    { at: { mode: "start", value: 0.5 }, value: home, easing: "linear" },
  ]);
}

function offscreenAtEnd(
  data: ScenarioData,
  id: string,
  property: Animatable,
  to: number,
): ScenarioData {
  const element = findElement(data, id);
  if (!element) return data;
  const home = staticOf(element, property);
  return setKeys(data, id, property, [
    { at: { mode: "end", offset_sec: -0.5 }, value: home, easing: "in" },
    { at: { mode: "end", offset_sec: 0 }, value: to, easing: "linear" },
  ]);
}

function byTime(a: ScenarioKeyframe, b: ScenarioKeyframe): number {
  return (a.at.value ?? 0) - (b.at.value ?? 0);
}

function round1(value: number): number {
  return Math.round(value * 10) / 10;
}
