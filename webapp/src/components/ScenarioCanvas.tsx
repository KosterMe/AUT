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
 * The spine is the exception and is not draggable. The compiler still reads
 * only its `fit` and gives every segment the rectangle that layout names
 * (trap 32), so letting somebody drag it would draw one thing and render
 * another. It says so rather than silently ignoring the mouse.
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
    if (block.track_kind === "spine") {
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
    if (block.track_kind === "spine") return;
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
          return (
            <div
              key={block.element_id}
              onPointerDown={(event) => startDrag(event, block, "move")}
              className={clsx(
                "absolute flex items-center justify-center text-[10px] font-medium text-white/90",
                SLOT_COLORS[block.slot_kind] ?? "bg-slate-500",
                spine ? "cursor-not-allowed opacity-80" : "cursor-move",
                isSelected ? "ring-2 ring-white" : "ring-1 ring-white/30",
              )}
              style={{
                left: `${rect.x - rect.width / 2}%`,
                top: `${rect.y - rect.height / 2}%`,
                width: `${rect.width}%`,
                height: `${rect.height}%`,
                opacity: spine ? 0.85 : 0.9,
              }}
              title={spine ? "Позвоночник рисуется раскладкой, а не прямоугольником" : block.label}
            >
              <span className="truncate px-1">{block.label}</span>
              {isSelected && !spine && (
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
