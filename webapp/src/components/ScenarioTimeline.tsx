import { useRef } from "react";
import clsx from "clsx";
import { AlertTriangle } from "lucide-react";
import type {
  InspectBlock,
  InspectKey,
  InspectRule,
  ScenarioData,
  ScenarioInspect,
} from "../api/types";
import {
  ANCHOR_GLYPHS,
  PROPERTY_LABELS,
  SLOT_COLORS,
  TRACK_LABELS,
  type Animatable,
} from "../pages/scenarioModel";

/**
 * The timeline, drawn from one compile of the draft.
 *
 * Three things here are not decoration. The glyph on a block says what it is
 * *held to*, so an element pinned to the end is visibly pinned to the right
 * edge rather than sitting at a number that happens to be large. A block that
 * did not fit is drawn as a block that did not fit, not as an absence —
 * absence is what a scenario looks like when it is fine. And what a rule made
 * is dashed, because it is where the rule fired on this material and will be
 * somewhere else on the next clip.
 */
export default function ScenarioTimeline({
  data,
  report,
  at,
  onScrub,
  selected,
  onSelect,
  onMoveKey,
  onMoveKeyEnd,
}: {
  data: ScenarioData;
  report: ScenarioInspect;
  at: number;
  onScrub: (at: number) => void;
  selected: string | null;
  onSelect: (id: string) => void;
  /** Dragging a diamond: the key's new second, as the lane measures it. */
  onMoveKey?: (property: Animatable, index: number, atSec: number) => void;
  onMoveKeyEnd?: () => void;
}) {
  const lane = useRef<HTMLDivElement>(null);
  const length = Math.max(1, report.timeline_sec);
  const chosen = report.blocks.find((block) => block.element_id === selected);

  const blocksByTrack = new Map<string, InspectBlock[]>();
  report.blocks.forEach((block) => {
    blocksByTrack.set(block.track_id, [...(blocksByTrack.get(block.track_id) ?? []), block]);
  });
  const rulesByTrack = new Map<string, InspectRule[]>();
  report.rules.forEach((rule) => {
    rulesByTrack.set(rule.track_id, [...(rulesByTrack.get(rule.track_id) ?? []), rule]);
  });

  function scrub(event: React.PointerEvent | PointerEvent) {
    const bounds = lane.current?.getBoundingClientRect();
    if (!bounds) return;
    const ratio = (event.clientX - bounds.left) / bounds.width;
    onScrub(Math.max(0, Math.min(length, ratio * length)));
  }

  function startScrub(event: React.PointerEvent) {
    scrub(event);
    function move(moveEvent: PointerEvent) {
      scrub(moveEvent);
    }
    function stop() {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
    }
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
  }

  return (
    <div className="space-y-1 select-none">
      <div className="flex">
        <div className="w-28 shrink-0" />
        {/* The ruler is measured rather than the rows: it spans exactly the
            lane, while a row also carries its label column. */}
        <div
          ref={lane}
          className="relative flex-1 h-6 cursor-ew-resize border-b border-slate-200"
          onPointerDown={startScrub}
        >
          {ticks(length).map((second) => (
            <span
              key={second}
              className="absolute top-0 text-[10px] text-slate-400 -translate-x-1/2"
              style={{ left: `${(second / length) * 100}%` }}
            >
              {second}
            </span>
          ))}
        </div>
      </div>

      <div className="relative">
        {data.tracks.map((track) => (
          <div key={track.id} className="flex items-stretch">
            <div className="w-28 shrink-0 py-1 pr-2 text-right">
              <span className="text-[11px] text-slate-500">{TRACK_LABELS[track.kind]}</span>
            </div>
            <div className="relative flex-1 h-9 border-b border-slate-100">
              {(blocksByTrack.get(track.id) ?? []).map((block) => (
                <Block
                  key={block.element_id}
                  block={block}
                  length={length}
                  selected={block.element_id === selected}
                  onSelect={onSelect}
                />
              ))}
            </div>
          </div>
        ))}

        {/* A rule gets its own lane rather than sharing the track's. It
            occupies no time of its own — what it makes does — and a rule that
            fired nowhere would otherwise be invisible and unselectable, which
            is the state in which somebody cannot find out why nothing
            happened. */}
        {data.tracks.flatMap((track) =>
          (rulesByTrack.get(track.id) ?? []).map((rule) => (
            <div key={rule.element_id} className="flex items-stretch">
              <div className="w-28 shrink-0 py-1 pr-2 text-right">
                <span className="text-[11px] text-emerald-700">правило</span>
              </div>
              <button
                onClick={() => onSelect(rule.element_id)}
                className={clsx(
                  "relative h-7 flex-1 border-b border-slate-100 text-left",
                  selected === rule.element_id && "bg-emerald-50/60",
                )}
                title={`${rule.label}: положение зависит от материала`}
              >
                <span className="absolute left-1 top-1 text-[10px] text-emerald-700">
                  {rule.label}
                  {rule.ghosts.length === 0 && " — на этом материале не сработало"}
                </span>
                {rule.ghosts.map((ghost, index) => (
                  <span
                    key={index}
                    className="absolute bottom-1 h-2.5 rounded border border-dashed
                               border-emerald-500 bg-emerald-100"
                    style={{
                      left: `${(ghost.at_sec / length) * 100}%`,
                      width: `${Math.max(0.5, (ghost.duration_sec / length) * 100)}%`,
                    }}
                  />
                ))}
              </button>
            </div>
          )),
        )}

        {/* Over the lanes only, so the percentage is of the lane and not of
            the row including its label column. */}
        <div className="pointer-events-none absolute inset-y-0 right-0 left-28">
          <div
            className="absolute inset-y-0 w-px bg-red-500"
            style={{ left: `${(at / length) * 100}%` }}
          />
        </div>
      </div>

      {chosen && chosen.keys.length > 0 && (
        <KeyLanes
          block={chosen}
          length={length}
          onMoveKey={onMoveKey}
          onMoveKeyEnd={onMoveKeyEnd}
        />
      )}

      <div className="flex gap-4 pl-28 pt-1 text-[11px] text-slate-500">
        <span>материал {report.material_sec.toFixed(0)} с</span>
        <span>сценарий {report.timeline_sec.toFixed(1)} с</span>
        <span
          className={clsx(
            Math.abs(report.duration_sec - report.timeline_sec) > 0.05 && "text-amber-600",
          )}
        >
          в файле {report.duration_sec.toFixed(1)} с
        </span>
        <span>субтитров {report.subtitle_count}</span>
        <span>раскладка {report.layout}</span>
      </div>
    </div>
  );
}

function Block({
  block,
  length,
  selected,
  onSelect,
}: {
  block: InspectBlock;
  length: number;
  selected: boolean;
  onSelect: (id: string) => void;
}) {
  if (!block.placed) {
    return (
      <button
        onClick={() => onSelect(block.element_id)}
        className={clsx(
          "absolute inset-y-1 left-0 flex items-center gap-1 rounded border border-dashed",
          "border-red-300 bg-red-50 px-2 text-[11px] text-red-700",
          selected && "ring-2 ring-red-400",
        )}
        title={block.note}
      >
        <AlertTriangle size={11} />
        {block.label}
      </button>
    );
  }

  return (
    <button
      onClick={() => onSelect(block.element_id)}
      className={clsx(
        "absolute inset-y-1 flex items-center gap-1 overflow-hidden rounded px-1.5",
        "text-[11px] font-medium text-white",
        SLOT_COLORS[block.slot_kind] ?? "bg-slate-500",
        block.optional && "opacity-70",
        selected && "ring-2 ring-slate-900",
      )}
      style={{
        left: `${(block.at_sec / length) * 100}%`,
        width: `${Math.max(1.5, (block.duration_sec / length) * 100)}%`,
      }}
      title={`${block.label} · ${block.at_sec.toFixed(1)}–${(
        block.at_sec + block.duration_sec
      ).toFixed(1)} с${block.note ? ` · ${block.note}` : ""}`}
    >
      <span className="opacity-80">{ANCHOR_GLYPHS[block.anchor]}</span>
      <span className="truncate">{block.label}</span>
    </button>
  );
}

/** Round tick marks, however long the clip is. */
function ticks(length: number): number[] {
  const step = length <= 40 ? 5 : length <= 100 ? 10 : 30;
  const out: number[] = [];
  for (let at = 0; at <= length + 0.001; at += step) out.push(Math.round(at));
  return out;
}


/**
 * The animation track: one lane per property that moves, with a diamond at
 * every key (§8.2).
 *
 * The seconds are the compiler's, not the editor's — a key written as "half a
 * second before the end" is drawn where that lands on *this* mock length, so
 * switching the layout switcher moves the diamond, which is the whole point of
 * anchoring keys instead of timing them.
 */
function KeyLanes({
  block,
  length,
  onMoveKey,
  onMoveKeyEnd,
}: {
  block: InspectBlock;
  length: number;
  onMoveKey?: (property: Animatable, index: number, atSec: number) => void;
  onMoveKeyEnd?: () => void;
}) {
  const byProperty = new Map<string, InspectKey[]>();
  block.keys.forEach((key) => {
    byProperty.set(key.property, [...(byProperty.get(key.property) ?? []), key]);
  });

  return (
    <div className="mt-1 border-t border-dashed border-slate-200 pt-1">
      {[...byProperty.entries()].map(([property, keys]) => (
        <div key={property} className="flex items-stretch">
          <div className="w-28 shrink-0 py-1 pr-2 text-right">
            <span className="text-[11px] text-brand-700">
              {PROPERTY_LABELS[property as Animatable] ?? property}
            </span>
          </div>
          <Lane
            keys={keys}
            length={length}
            onMove={
              onMoveKey && ((index, at) => onMoveKey(property as Animatable, index, at))
            }
            onMoveEnd={onMoveKeyEnd}
          />
        </div>
      ))}
    </div>
  );
}

function Lane({
  keys,
  length,
  onMove,
  onMoveEnd,
}: {
  keys: InspectKey[];
  length: number;
  onMove?: (index: number, atSec: number) => void;
  onMoveEnd?: () => void;
}) {
  const lane = useRef<HTMLDivElement>(null);

  function drag(event: React.PointerEvent, index: number, key: InspectKey) {
    if (!onMove) return;
    event.preventDefault();
    event.stopPropagation();
    const bounds = lane.current?.getBoundingClientRect();
    if (!bounds) return;
    // An end-anchored key has no absolute second to set: what moves is how
    // far before the end it sits, which is a negative number.
    const toValue = (clientX: number) => {
      const at = ((clientX - bounds.left) / bounds.width) * length;
      const clamped = Math.max(0, Math.min(length, at));
      return key.anchor === "end" ? clamped - length : clamped;
    };

    function move(moveEvent: PointerEvent) {
      onMove!(index, Math.round(toValue(moveEvent.clientX) * 10) / 10);
    }
    function stop() {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      onMoveEnd?.();
    }
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
  }

  return (
    <div ref={lane} className="relative h-6 flex-1 border-b border-slate-100">
      {keys.map((key, index) => (
        <span
          key={index}
          onPointerDown={(event) => drag(event, index, key)}
          className="absolute top-1.5 h-3 w-3 -translate-x-1/2 rotate-45 cursor-ew-resize
                     border border-brand-600 bg-white"
          style={{ left: `${(key.at_sec / length) * 100}%` }}
          title={`${key.at_sec.toFixed(1)} с · ${key.value.toFixed(0)}% · ${key.easing}` +
            (key.anchor === "end" ? " · прижат к концу" : "")}
        />
      ))}
      {keys.length > 1 && (
        <span
          className="absolute top-3 h-px bg-brand-200"
          style={{
            left: `${(keys[0].at_sec / length) * 100}%`,
            width: `${((keys[keys.length - 1].at_sec - keys[0].at_sec) / length) * 100}%`,
          }}
        />
      )}
    </div>
  );
}
