/**
 * The shell's shared bottom corner (#510): the download tray, the update prompt, and
 * the toast cards all render as flow children of this ONE positioned column. Corner
 * surfaces must never pin their own `fixed` box — independent fixed boxes at the same
 * coordinates don't stack, they occlude (z-index picks a winner and the rest sit
 * invisible underneath). A flex column gives simultaneity for free: whatever is
 * mounted stacks upward from the corner.
 *
 * z-70 keeps the whole region above the Confirm layer (z-60) — the Toaster's old rule
 * (a failure raised from a dialog action must never hide behind it), now shared by
 * every corner surface. `pointer-events-none` on the region with `pointer-events-auto`
 * per card, so the empty column never swallows clicks meant for the page.
 */
import type { ReactNode } from "react";

export function CornerStack({ children }: { children: ReactNode }) {
  return (
    <div className="pointer-events-none fixed inset-x-4 bottom-20 z-70 flex flex-col gap-2 sm:inset-x-auto sm:bottom-6 sm:right-6 sm:w-80">
      {children}
    </div>
  );
}
