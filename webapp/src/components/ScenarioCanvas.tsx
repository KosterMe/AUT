import { useRef, useState } from "react";
import clsx from "clsx";
import type { InspectBlock, ScenarioData } from "../api/types";
import { SLOT_COLORS, findElement, numberOf } from "../pages/scenarioModel";

/**
 * The canvas: a 9:16 rectangle with the elements that are on screen at one
 * moment, in the proportions they will render at.
 *
 * Two sources, on purpose. *Which* elements are visible and *when* comes from
 * the compiler, through `inspect` — the editor never works that out itself.
 * Where each rectangle sits comes from the draft while it is being dragged,
 * because a rectangle that waited for a round trip before following the mouse
 * is not direct manipulation, it is a form with a slow submit button.
 *
 * The spine can be dragged like anything else now (trap 32 is closed): the
 * rectangle it carries reaches the segments. What it cannot do is *move* —
 * a segment is framed before the join, where every segment's clock starts
 * again — so a spine with keyframes is refused by the compiler and drawn
 * here as the still rectangle it will actually be.
 */
export default function ScenarioCanvas({
  data,
  blocks,
  at,
  selected,
  onSelect,
  onMove,
  onMoveEnd,
}: {
  data: ScenarioData;
  blocks: InspectBlock[];
  at: number;
  selected: string | null;
  onSelect: (id: string) => void;
  onMove: (id: string, rect: Rect) => void;
  /** The gesture ended: what follows is a separate undo step. */
  onMoveEnd?: () => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [guides, setGuides] = useState<{ x: number[]; y: number[] }>({ x: [], y: [] });

  const visible = blocks
    .filter((block) => block.track_kind !== "audio" && block.placed)
    .filter((block) => at >= block.at_sec - 0.001 && at < block.at_sec + block.duration_sec)
    .sort((a, b) => a.z - b.z);

  function rectOf(block: InspectBlock): Rect {
    // A spine left alone carries the default rectangle, which means "the
    // layout decides" and comes back from the compile as whatever it decided;
    // a moving element only exists at a moment. The draft is consulted for
    // anything still and dragged, and only so that it follows the mouse
    // instead of the network.
    if ((block.track_kind === "spine" && !draggedSpine(data, block)) || block.frame.moving) {
      return {
        x: block.frame.x,
        y: block.frame.y,
        width: block.frame.width,
        height: block.frame.height,
      };
    }
    const frame = findElement(data, block.element_id)?.frame;
    return {
      x: numberOf(frame?.x, block.frame.x),
      y: numberOf(frame?.y, block.frame.y),
      width: numberOf(frame?.width, block.frame.width),
      height: numberOf(frame?.height, block.frame.height),
    };
  }

  function startDrag(
    event: React.PointerEvent,
    block: InspectBlock,
    handle: Handle,
  ) {
    // A moving element is not dragged: its position at this moment is one
    // frame of a curve, and dropping it somewhere would have to mean editing
    // the curve — which is what the keyframe lane is for.
    if (block.frame.moving) return;
    event.preventDefault();
    event.stopPropagation();
    onSelect(block.element_id);

    const bounds = box.current?.getBoundingClientRect();
    if (!bounds) return;
    const start = rectOf(block);
    const fromX = event.clientX;
    const fromY = event.clientY;
    const target = event.currentTarget as HTMLElement;
    target.setPointerCapture(event.pointerId);

    function move(moveEvent: PointerEvent) {
      const dx = ((moveEvent.clientX - fromX) / bounds!.width) * 100;
      const dy = ((moveEvent.clientY - fromY) / bounds!.height) * 100;
      const dragged = apply(start, handle, dx, dy);
      const snapped = snap(dragged, moveEvent.shiftKey);
      setGuides(snapped.guides);
      onMove(block.element_id, snapped.rect);
    }

    function stop() {
      setGuides({ x: [], y: [] });
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      onMoveEnd?.();
    }

    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
  }

  return (
    <div className="space-y-2">
      <div
        ref={box}
        className="relative mx-auto w-full max-w-[280px] aspect-[9/16] rounded-lg bg-slate-900
                   overflow-hidden select-none touch-none"
        onPointerDown={() => onSelect("")}
      >
        {visible.map((block) => {
          const rect = rectOf(block);
          const isSelected = block.element_id === selected;
          const spine = block.track_kind === "spine";
          const fixed = block.frame.moving;
          return (
            <div
              key={block.element_id}
              onPointerDown={(event) => startDrag(event, block, "move")}
              className={clsx(
                "absolute flex items-center justify-center text-[10px] font-medium text-white/90",
                SLOT_COLORS[block.slot_kind] ?? "bg-slate-500",
                fixed ? "cursor-not-allowed opacity-80" : "cursor-move",
                isSelected ? "ring-2 ring-white" : "ring-1 ring-white/30",
              )}
              style={{
                left: `${rect.x - rect.width / 2}%`,
                top: `${rect.y - rect.height / 2}%`,
                width: `${rect.width}%`,
                height: `${rect.height}%`,
                // Degrees, and the same angle the renderer turns it by — the
                // sample comes from the compile rather than from the draft.
                transform: block.frame.rotate ? `rotate(${block.frame.rotate}deg)` : undefined,
                // The block is drawn slightly see-through as a matter of
                // chrome, and its own opacity multiplies that — so an element
                // fading in reads as fading rather than as solid until the
                // render disagrees. Floored, because a block at zero would
                // otherwise be invisible and therefore unselectable, and the
                // moment you most want to click it is the one where it has
                // faded out.
                opacity: Math.max(0.15, (spine ? 0.85 : 0.9) * (block.frame.opacity ?? 1)),
              }}
              title={
                block.frame.moving
                  ? `${block.label}: это один кадр движения — правится ключами`
                  : spine && !draggedSpine(data, block)
                    ? `${block.label}: рамку выбрала раскладка — потяните, и она станет вашей`
                    : block.label
              }
            >
              <span className="truncate px-1">{block.label}</span>
              {isSelected && !fixed && (
                <>
                  {(["nw", "ne", "sw", "se"] as Handle[]).map((handle) => (
                    <span
                      key={handle}
                      onPointerDown={(event) => startDrag(event, block, handle)}
                      className={clsx(
                        "absolute h-2.5 w-2.5 rounded-sm bg-white ring-1 ring-slate-400",
                        handle === "nw" && "-left-1 -top-1 cursor-nwse-resize",
                        handle === "ne" && "-right-1 -top-1 cursor-nesw-resize",
                        handle === "sw" && "-bottom-1 -left-1 cursor-nesw-resize",
                        handle === "se" && "-bottom-1 -right-1 cursor-nwse-resize",
                      )}
                    />
                  ))}
                </>
              )}
            </div>
          );
        })}

        {guides.x.map((x) => (
          <div key={`x${x}`} className="absolute inset-y-0 w-px bg-amber-300" style={{ left: `${x}%` }} />
        ))}
        {guides.y.map((y) => (
          <div key={`y${y}`} className="absolute inset-x-0 h-px bg-amber-300" style={{ top: `${y}%` }} />
        ))}

        {visible.length === 0 && (
          <p className="absolute inset-0 flex items-center justify-center text-xs text-slate-400">
            В этот момент ничего нет
          </p>
        )}
      </div>
      <p className="text-center text-[11px] text-slate-400">
        1080×1920 · {at.toFixed(1)} с
      </p>
    </div>
  );
}

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

type Handle = "move" | "nw" | "ne" | "sw" | "se";

function apply(start: Rect, handle: Handle, dx: number, dy: number): Rect {
  if (handle === "move") return { ...start, x: start.x + dx, y: start.y + dy };

  const left = start.x - start.width / 2;
  const top = start.y - start.height / 2;
  let [x0, y0, x1, y1] = [left, top, left + start.width, top + start.height];
  if (handle === "nw" || handle === "sw") x0 += dx;
  if (handle === "ne" || handle === "se") x1 += dx;
  if (handle === "nw" || handle === "ne") y0 += dy;
  if (handle === "sw" || handle === "se") y1 += dy;

  const width = Math.max(5, x1 - x0);
  const height = Math.max(5, y1 - y0);
  return { x: x0 + width / 2, y: y0 + height / 2, width, height };
}

// Edges, centres and thirds. Thirds because that is where a face goes when it
// is not in the middle, and eyeballing 33.3 with a mouse is not a thing anyone
// can do.
const STOPS = [0, 33.3, 50, 66.7, 100];
const THRESHOLD = 1.5;

function snap(rect: Rect, off: boolean): { rect: Rect; guides: { x: number[]; y: number[] } } {
  if (off) return { rect, guides: { x: [], y: [] } };

  const guides: { x: number[]; y: number[] } = { x: [], y: [] };
  let { x, y } = rect;

  for (const edge of [x - rect.width / 2, x, x + rect.width / 2]) {
    const stop = STOPS.find((candidate) => Math.abs(edge - candidate) < THRESHOLD);
    if (stop !== undefined) {
      x += stop - edge;
      guides.x.push(stop);
      break;
    }
  }
  for (const edge of [y - rect.height / 2, y, y + rect.height / 2]) {
    const stop = STOPS.find((candidate) => Math.abs(edge - candidate) < THRESHOLD);
    if (stop !== undefined) {
      y += stop - edge;
      guides.y.push(stop);
      break;
    }
  }
  return { rect: { ...rect, x, y }, guides };
}


/**
 * Whether the spine carries a rectangle somebody put there.
 *
 * The default rectangle is not "middle, full size": it is the compiler's way
 * of saying the layout decides (`auto` meeting the shape of the source, a
 * blurred backdrop, a split). Drawing the draft's numbers while that is true
 * would show a full-canvas box over a picture that is actually contained in
 * one — so until it is dragged, the spine is drawn from the compile.
 */
function draggedSpine(data: ScenarioData, block: InspectBlock): boolean {
  const frame = findElement(data, block.element_id)?.frame;
  if (!frame) return false;
  return !(
    numberOf(frame.x, 50) === 50 &&
    numberOf(frame.y, 50) === 50 &&
    numberOf(frame.width, 100) === 100 &&
    numberOf(frame.height, 100) === 100
  );
}
