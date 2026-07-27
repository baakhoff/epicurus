import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/react";

// jsdom implements no layout, so it ships no scrollIntoView — any component that follows
// a tail (the Observability consoles, chat) throws "not a function" on mount under test
// while working fine in a browser. Stub it globally rather than making components defend
// against the test environment.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

// testing-library's default findBy*/waitFor timeout (1000ms) assumes a lightly-loaded
// machine. Under a full-suite run (~92 files across parallel workers) a mocked promise
// that resolves in a microtask under normal conditions can occasionally take longer than
// that just from CPU scheduling pressure, not from an actual bug — a real full-suite
// flake (services/web/src/test/AutomationsScreen.test.tsx, #758) traced to exactly this.
// 3s keeps a genuinely-hung query failing promptly while giving contention enough room.
configure({ asyncUtilTimeout: 3000 });
