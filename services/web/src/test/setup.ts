import "@testing-library/jest-dom/vitest";

// jsdom implements no layout, so it ships no scrollIntoView — any component that follows
// a tail (the Observability consoles, chat) throws "not a function" on mount under test
// while working fine in a browser. Stub it globally rather than making components defend
// against the test environment.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}
