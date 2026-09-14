/**
 * The editor's model: pure functions over the draft.
 *
 * Until now the whole frontend was checked by opening it in a browser, and
 * that verified the screen — not the rules underneath it. The rules are where
 * a mistake is silent: a scenario saved with one keyframe quietly attached to
 * the wrong property still renders, still opens, and is simply not the montage
 * somebody assembled.
 *
 * Nothing here renders a component. What a component does with these values is
 * a question for the browser (§8.5); what the values *are* is a question that
 * has an answer, and this is it.
 */
import { describe, expect, it } from "vitest";
import type { ScenarioData, ScenarioElement } from "../api/types";
import {
  ANIMATABLE,
  MOTION_PRESETS,
  PROPERTY_LABELS,
  SCALES,
  addElement,
  emptyScenario,
  findElement,
  keysOf,
  newElement,
  putKey,
  removeElement,
  removeKey,
  setKeys,
  staticOf,
  trackFor,
  updateElement,
  type Animatable,
} from "./scenarioModel";

/** A draft with one overlay element to animate. */
function draft(): ScenarioData {
  const data = emptyScenario("проба");
  const element = newElement(data, "library");
  // `trackFor` picks the track, which is the model's decision and not this
  // test's: an overlay added to the spine would be a different scenario.
  const placed = { ...element, id: "over" };
  return addElement(data, placed, trackFor(data, placed));
}

function over(data: ScenarioData): ScenarioElement {
  const element = findElement(data, "over");
  if (!element) throw new Error("the element under test went missing");
  return element;
}

describe("a value that carries keys", () => {
  it("keeps the number it was, for when the keys are taken away", () => {
    // `is_static` is the cheap path a scenario without animation must keep
    // (§4.3), and `static` is what the element is worth once the keys go. A
    // model that dropped it would silently reset the element to its resting
    // value the moment somebody cleared the animation.
    //
    // The width is moved off its resting value first, and that is the point
    // of the line: with the element left at 100 — which is what `RESTING`
    // says — this test passed against a model that substituted the resting
    // value for the real one, because the two were the same number.
    const narrowed = updateElement(draft(), "over", {
      frame: { ...over(draft()).frame, width: 35 },
    });

    const data = setKeys(narrowed, "over", "width", [
      { at: { mode: "start", value: 0 }, value: 40 },
      { at: { mode: "start", value: 2 }, value: 80 },
    ]);

    expect(over(data).frame?.width).toMatchObject({ static: 35 });
    expect(keysOf(over(data), "width")).toHaveLength(2);
    expect(staticOf(over(setKeys(data, "over", "width", [])), "width")).toBe(35);
  });

  it("collapses back to a plain number when the last key goes", () => {
    const keyed = setKeys(draft(), "over", "x", [
      { at: { mode: "start", value: 0 }, value: 20 },
    ]);

    const bare = removeKey(keyed, "over", "x", 0);

    expect(over(bare).frame?.x).toBe(50);
    expect(staticOf(over(bare), "x")).toBe(50);
  });

  it("leaves the other properties alone", () => {
    const data = setKeys(draft(), "over", "opacity", [
      { at: { mode: "start", value: 0 }, value: 0 },
    ]);

    expect(over(data).frame?.width).toBe(100);
    expect(over(data).frame?.fit).toBe("cover");
  });
});

describe("putting a key at the playhead", () => {
  it("replaces the one already there rather than stacking a second", () => {
    // Pressing "+ key" twice without moving the playhead is an ordinary
    // slip, and two keys on one second is a zero-length stretch of curve.
    const once = putKey(draft(), "over", "x", 2, 30);
    const twice = putKey(once, "over", "x", 2, 70);

    expect(keysOf(over(twice), "x")).toEqual([
      { at: { mode: "start", value: 2 }, value: 70, easing: "linear" },
    ]);
  });

  it("counts near enough as the same second", () => {
    const data = putKey(putKey(draft(), "over", "x", 2, 30), "over", "x", 2.03, 70);

    expect(keysOf(over(data), "x")).toHaveLength(1);
  });

  it("puts a key added earlier in front of one added before it", () => {
    const later = putKey(draft(), "over", "x", 4, 80);
    const data = putKey(later, "over", "x", 1, 20);

    expect(keysOf(over(data), "x").map((key) => key.at.value)).toEqual([1, 4]);
  });

  it("rounds to a tenth, because that is what the timeline can point at", () => {
    const data = putKey(draft(), "over", "x", 1.2345, 33.333);

    expect(keysOf(over(data), "x")[0]).toMatchObject({
      at: { mode: "start", value: 1.2 }, value: 33.3,
    });
  });
});

describe("the draft is replaced, never edited in place", () => {
  // React decides what to redraw by identity. A function that mutated the
  // draft would leave the screen showing the state before the edit, which
  // reads as "the click did nothing".
  it("updating an element leaves the original as it was", () => {
    const before = draft();
    const after = updateElement(before, "over", { label: "Другое" });

    expect(over(before).label).toBe("Из библиотеки");
    expect(over(after).label).toBe("Другое");
    expect(after).not.toBe(before);
  });

  it("removing an element leaves the original as it was", () => {
    const before = draft();
    const after = removeElement(before, "over");

    expect(findElement(before, "over")).toBeDefined();
    expect(findElement(after, "over")).toBeUndefined();
  });

  it("setting keys leaves the original as it was", () => {
    const before = draft();
    const after = setKeys(before, "over", "rotate", [
      { at: { mode: "start", value: 0 }, value: 15 },
    ]);

    expect(keysOf(over(before), "rotate")).toEqual([]);
    expect(keysOf(over(after), "rotate")).toHaveLength(1);
  });
});

describe("a new element states only what it means to", () => {
  // The server's decoder fills in the model's own defaults for anything left
  // out. A default written here too is a second copy, and the two drift —
  // after which the editor is quietly overriding the model with a stale idea
  // of what it wanted.
  it("does not spell out a frame it has no opinion about", () => {
    const data = emptyScenario("проба");

    expect(newElement(data, "color").frame).toBeUndefined();
    expect(newElement(data, "text").frame).toBeUndefined();
  });

  it("gives a library slot the tag the model insists on", () => {
    // The model refuses a library slot with nothing to look for, so an
    // element without one could be built here and never saved.
    expect(newElement(emptyScenario("проба"), "library").slot).toMatchObject({
      kind: "library", tag: "broll",
    });
  });

  it("does not collide with an id already taken", () => {
    const one = draft();
    const another = newElement(one, "library");
    const two = addElement(one, another, trackFor(one, another));

    const ids = two.tracks.flatMap((track) => track.elements ?? []).map((e) => e.id);

    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe("the lists of properties agree with each other", () => {
  // Every one of these is a name written out a second time, and the project
  // has been bitten three times by exactly that (traps 25, 53, 56). They are
  // cheap to compare and silent when they disagree: a property missing from
  // `RESTING` reads as 0, which for a width means "invisible".
  it.each(ANIMATABLE)("%s has a label", (property) => {
    expect(PROPERTY_LABELS[property]).toBeTruthy();
  });

  it.each(ANIMATABLE)("%s says how finely it is typed", (property) => {
    expect(SCALES[property]?.step).toBeGreaterThan(0);
  });

  it("gives the one property on a 0..1 scale a step it can reach", () => {
    // Stepping a share of one by one leaves two usable values.
    expect(SCALES.opacity).toEqual({ step: 0.05, min: 0, max: 1 });
  });

  it("has a resting value for each, and the right one for height", () => {
    const data = emptyScenario("проба");
    const element = newElement(data, "color");

    // Zero is not "flat": it is "the aspect ratio decides", which is why the
    // table exists rather than one number.
    expect(staticOf(element, "height")).toBe(0);
    expect(staticOf(element, "width")).toBe(100);
    expect(staticOf(element, "opacity")).toBe(1);
  });
});

describe("motion presets", () => {
  // Each preset declares the property it writes on, and the editor now hides
  // presets whose property this build of ffmpeg cannot animate (§7.2). A
  // preset that declared one property and wrote another would be offered on a
  // build that cannot render what it does.
  it.each(MOTION_PRESETS)("$name writes keys on the property it declares", (preset) => {
    const data = preset.apply(draft(), "over");
    const touched = ANIMATABLE.filter(
      (property) => keysOf(over(data), property).length > 0,
    );

    expect(touched).toEqual([preset.property]);
  });

  it.each(MOTION_PRESETS)("$name declares a property the editor knows", (preset) => {
    expect(ANIMATABLE).toContain(preset.property);
  });

  it.each(MOTION_PRESETS)("$name puts down at least two keys", (preset) => {
    // A preset is a movement, and one key is a constant. The panel says so —
    // "a preset puts down two at once" — and a preset that put down one would
    // make it a lie in the quietest possible way.
    const data = preset.apply(draft(), "over");

    expect(keysOf(over(data), preset.property).length).toBeGreaterThanOrEqual(2);
  });

  it("leaves the element where it was when the preset is about arriving", () => {
    // "Въезд слева" arrives at wherever the element sits, so the last key has
    // to be the element's own position rather than a number written here.
    const start = putKey(draft(), "over", "x", 0, 0);
    const placed = setKeys(start, "over", "x", []);
    const moved = updateElement(placed, "over", {
      frame: { ...over(placed).frame, x: 30 },
    });

    const data = MOTION_PRESETS.find((p) => p.name === "Въезд слева")!.apply(moved, "over");
    const keys = keysOf(over(data), "x");

    expect(keys[keys.length - 1].value).toBe(30);
  });
});

describe("clearing animation", () => {
  it("takes away the keys of one property and leaves the rest", () => {
    let data = MOTION_PRESETS.find((p) => p.name === "Проявление")!.apply(draft(), "over");
    data = MOTION_PRESETS.find((p) => p.name === "Въезд слева")!.apply(data, "over");

    const cleared = setKeys(data, "over", "opacity", []);

    expect(keysOf(over(cleared), "opacity")).toEqual([]);
    expect(keysOf(over(cleared), "x").length).toBeGreaterThan(0);
  });
});

describe("an element that is not there", () => {
  // Every editing function takes an id, and the panel can outlive the element
  // it was showing — an undo, a delete from the timeline. Returning the draft
  // unchanged is the answer; throwing would take the screen with it.
  const edits: [string, (data: ScenarioData) => ScenarioData][] = [
    ["setKeys", (d) => setKeys(d, "ghost", "x", [{ at: { mode: "start", value: 0 }, value: 1 }])],
    ["putKey", (d) => putKey(d, "ghost", "x", 1, 2)],
    ["removeKey", (d) => removeKey(d, "ghost", "x", 0)],
    ["updateElement", (d) => updateElement(d, "ghost", { label: "x" })],
    ["removeElement", (d) => removeElement(d, "ghost")],
  ];

  it.each(edits)("%s leaves the draft alone", (_name, edit) => {
    const before = draft();

    expect(edit(before)).toEqual(before);
  });
});

describe("what the editor knows about properties", () => {
  it("knows exactly six, and opacity is one of them", () => {
    // The server holds the same list as `composition.CURVES` and a test in
    // the Python suite compares the two, so this one only has to notice that
    // somebody edited this end of it.
    expect([...ANIMATABLE]).toEqual([
      "x", "y", "width", "height", "rotate", "opacity",
    ]);
  });

  it("reads a property with no keys as still", () => {
    const element = newElement(emptyScenario("проба"), "color");

    for (const property of ANIMATABLE as readonly Animatable[]) {
      expect(keysOf(element, property)).toEqual([]);
    }
  });
});
