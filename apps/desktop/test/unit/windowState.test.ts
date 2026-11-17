import { describe, expect, it } from "vitest";

import { DEFAULT_WINDOW, MIN_HEIGHT, MIN_WIDTH } from "../../src/main/settings";
import { restoreBounds, type Rect } from "../../src/main/windowState";

const laptop: Rect = { x: 0, y: 0, width: 1440, height: 875 };
const rightMonitor: Rect = { x: 1440, y: 0, width: 2560, height: 1415 };

describe("restoreBounds", () => {
  it("uses the default size, centred, on first run", () => {
    expect(restoreBounds(null, [laptop])).toEqual({
      width: DEFAULT_WINDOW.width,
      height: DEFAULT_WINDOW.height,
      maximized: false,
    });
  });

  it("restores a saved position that is on a display", () => {
    expect(restoreBounds({ x: 1600, y: 100, width: 1200, height: 900, maximized: true }, [laptop, rightMonitor]))
      .toEqual({ x: 1600, y: 100, width: 1200, height: 900, maximized: true });
  });

  it("drops a position on a display that is gone, and clamps the size to what is left", () => {
    expect(restoreBounds({ x: 1600, y: 100, width: 2400, height: 1300, maximized: false }, [laptop]))
      .toEqual({ width: 1440, height: 875, maximized: false });
  });

  it("keeps a window that hangs partly off screen as long as its title strip is reachable", () => {
    expect(restoreBounds({ x: -100, y: 10, width: 900, height: 700, maximized: false }, [laptop]))
      .toMatchObject({ x: -100, y: 10 });
    expect(restoreBounds({ x: -900, y: 10, width: 900, height: 700, maximized: false }, [laptop]))
      .not.toHaveProperty("x");
    expect(restoreBounds({ x: 10, y: -500, width: 900, height: 700, maximized: false }, [laptop]))
      .not.toHaveProperty("y");
  });

  it("never goes below the minimum size, even with no display information", () => {
    expect(restoreBounds({ width: 10, height: 10, maximized: false }, [])).toEqual({
      width: MIN_WIDTH,
      height: MIN_HEIGHT,
      maximized: false,
    });
  });
});
