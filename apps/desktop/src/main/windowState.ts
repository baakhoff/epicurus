/** Restoring the window where it was — without restoring it off screen. */
import { DEFAULT_WINDOW, MIN_HEIGHT, MIN_WIDTH, type WindowState } from "./settings";

export type Rect = { x: number; y: number; width: number; height: number };

/** How much of the window's title strip must land on a display for its position to be kept. */
const VISIBLE_STRIP = { width: 120, height: 32 };

function intersects(a: Rect, b: Rect): boolean {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}

/**
 * The bounds to open the window with, given the saved state and the displays' work areas.
 *
 * Size is kept but clamped to the largest work area (a window saved on a big monitor still fits
 * on a laptop). The position is kept only when the window's title strip would land on a display
 * that exists now; otherwise it is dropped and the system centres the window.
 */
export function restoreBounds(
  saved: WindowState | null,
  workAreas: readonly Rect[],
): { x?: number; y?: number; width: number; height: number; maximized: boolean } {
  const state = saved ?? DEFAULT_WINDOW;
  const maxWidth = Math.max(MIN_WIDTH, ...workAreas.map((a) => a.width));
  const maxHeight = Math.max(MIN_HEIGHT, ...workAreas.map((a) => a.height));
  const width = Math.min(Math.max(state.width, MIN_WIDTH), maxWidth);
  const height = Math.min(Math.max(state.height, MIN_HEIGHT), maxHeight);
  const result: { x?: number; y?: number; width: number; height: number; maximized: boolean } = {
    width,
    height,
    maximized: state.maximized,
  };
  if (state.x !== undefined && state.y !== undefined) {
    const strip: Rect = {
      x: state.x,
      y: state.y,
      width: Math.min(VISIBLE_STRIP.width, width),
      height: VISIBLE_STRIP.height,
    };
    if (workAreas.some((area) => intersects(strip, area))) {
      result.x = state.x;
      result.y = state.y;
    }
  }
  return result;
}
