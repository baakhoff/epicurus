/**
 * Mirrors the true visual viewport into CSS custom properties `#root` reads (index.css).
 *
 * `--app-height`: on Android PWA, 100dvh can misreport for a moment right after a reload,
 * pushing the fixed shell — and the bottom nav pinned to it — below the fold (#429).
 *
 * `--app-offset-top`: the #476 keyboard fallback. On an engine that ignores the viewport
 * meta's `interactive-widget=resizes-content`, opening the on-screen keyboard *pans* the
 * visual viewport within an unchanged layout viewport (`offsetTop` becomes non-zero) instead
 * of resizing it — `#root`'s `translateY` cancels that pan back out. Zero-cost on an engine
 * where the primary fix already prevents the pan (`offsetTop` never moves).
 *
 * Extracted from `App.tsx` so it's unit-testable without mounting the whole app shell.
 */
import { useEffect } from "react";

export function useViewportMirror(): void {
  useEffect(() => {
    const vv = window.visualViewport;
    const sync = () => {
      document.documentElement.style.setProperty(
        "--app-height",
        `${vv?.height ?? window.innerHeight}px`,
      );
      document.documentElement.style.setProperty("--app-offset-top", `${vv?.offsetTop ?? 0}px`);
    };
    sync();
    // Confusingly, the visual viewport's own offset changes fire as a `scroll` event, not
    // `resize` — the spec models "the keyboard opened and panned the view" the same as a
    // user scrolling the visual viewport around within the layout viewport, which (outside
    // pinch-zoom) is exactly what a keyboard-avoidance pan is.
    vv?.addEventListener("resize", sync);
    vv?.addEventListener("scroll", sync);
    window.addEventListener("resize", sync);
    return () => {
      vv?.removeEventListener("resize", sync);
      vv?.removeEventListener("scroll", sync);
      window.removeEventListener("resize", sync);
    };
  }, []);
}
