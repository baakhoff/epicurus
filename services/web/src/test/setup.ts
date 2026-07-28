import "@testing-library/jest-dom/vitest";

// jsdom implements no layout, so it ships no scrollIntoView — any component that follows
// a tail (the Observability consoles, chat) throws "not a function" on mount under test
// while working fine in a browser. Stub it globally rather than making components defend
// against the test environment.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

// jsdom doesn't implement the Pointer Capture APIs either (#730's resizable-panel drag
// calls setPointerCapture/releasePointerCapture on pointerdown/pointerup) — every evergreen
// browser has them, so stub rather than guard the component against the test environment.
if (!Element.prototype.setPointerCapture) {
  Element.prototype.setPointerCapture = () => {};
}
if (!Element.prototype.releasePointerCapture) {
  Element.prototype.releasePointerCapture = () => {};
}
