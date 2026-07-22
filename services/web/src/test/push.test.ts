import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mockVapidKey = vi.fn();
vi.mock("@/lib/api", () => ({
  api: {
    pushVapidPublicKey: (...a: unknown[]) => mockVapidKey(...a),
  },
}));

import {
  getExistingSubscription,
  guessDeviceLabel,
  isPushSupported,
  subscribeThisDevice,
  unsubscribeThisDevice,
} from "@/lib/push";

function stubServiceWorker(pushManager: unknown) {
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: { ready: Promise.resolve({ pushManager }) },
  });
}

function stubNoServiceWorker() {
  // Feature detection uses `"serviceWorker" in navigator` — a real unsupported browser has no
  // such property at all, so this must delete it, not set it to undefined (which would leave
  // the property present with a falsy value, and `in` would still see it as supported).
  delete (navigator as { serviceWorker?: unknown }).serviceWorker;
}

beforeEach(() => {
  mockVapidKey.mockReset().mockResolvedValue({ public_key: "BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U" });
  vi.stubGlobal("PushManager", class {});
  vi.stubGlobal(
    "Notification",
    class {
      static permission = "default";
      static requestPermission = vi.fn().mockResolvedValue("granted");
    },
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete (navigator as { serviceWorker?: unknown }).serviceWorker;
});

describe("isPushSupported", () => {
  it("is true when serviceWorker + PushManager + Notification are all present", () => {
    stubServiceWorker({});
    expect(isPushSupported()).toBe(true);
  });

  it("is false without serviceWorker (e.g. Safari desktop, some contexts)", () => {
    stubNoServiceWorker();
    expect(isPushSupported()).toBe(false);
  });

  it("is false without PushManager (e.g. iOS Safari below 16.4, non-installed PWA)", () => {
    stubServiceWorker({});
    // Same `in`-vs-undefined pitfall as serviceWorker above: stubbing to undefined would
    // still leave the property present, so delete it outright to simulate real absence.
    delete (globalThis as { PushManager?: unknown }).PushManager;
    expect(isPushSupported()).toBe(false);
  });
});

describe("getExistingSubscription", () => {
  it("returns null when unsupported", async () => {
    stubNoServiceWorker();
    await expect(getExistingSubscription()).resolves.toBeNull();
  });

  it("returns the registration's subscription when supported", async () => {
    const fakeSub = { endpoint: "https://push.example/abc" };
    stubServiceWorker({ getSubscription: vi.fn().mockResolvedValue(fakeSub) });
    await expect(getExistingSubscription()).resolves.toBe(fakeSub);
  });

  it("returns null when supported but not subscribed", async () => {
    stubServiceWorker({ getSubscription: vi.fn().mockResolvedValue(null) });
    await expect(getExistingSubscription()).resolves.toBeNull();
  });
});

describe("subscribeThisDevice", () => {
  it("returns null when unsupported, without prompting for permission", async () => {
    stubNoServiceWorker();
    await expect(subscribeThisDevice()).resolves.toBeNull();
  });

  it("returns null when permission is denied, without calling the backend", async () => {
    stubServiceWorker({ subscribe: vi.fn() });
    vi.stubGlobal(
      "Notification",
      class {
        static requestPermission = vi.fn().mockResolvedValue("denied");
      },
    );
    await expect(subscribeThisDevice()).resolves.toBeNull();
    expect(mockVapidKey).not.toHaveBeenCalled();
  });

  it("fetches the VAPID key and subscribes with it, once permission is granted", async () => {
    const fakeSub = { endpoint: "https://push.example/new" };
    const subscribe = vi.fn().mockResolvedValue(fakeSub);
    stubServiceWorker({ subscribe });

    const result = await subscribeThisDevice();

    expect(result).toBe(fakeSub);
    expect(mockVapidKey).toHaveBeenCalledOnce();
    expect(subscribe).toHaveBeenCalledOnce();
    const options = subscribe.mock.calls[0][0];
    expect(options.userVisibleOnly).toBe(true);
    // The base64url public key decodes to the raw uncompressed-point bytes the Push API wants
    // — not the string itself (a common integration bug: passing the string straight through).
    expect(options.applicationServerKey).toBeInstanceOf(Uint8Array);
    expect(options.applicationServerKey.length).toBeGreaterThan(0);
  });
});

describe("unsubscribeThisDevice", () => {
  it("returns true (no-op) when there is no existing subscription", async () => {
    stubServiceWorker({ getSubscription: vi.fn().mockResolvedValue(null) });
    await expect(unsubscribeThisDevice()).resolves.toBe(true);
  });

  it("unsubscribes the existing subscription and returns its result", async () => {
    const unsubscribe = vi.fn().mockResolvedValue(true);
    stubServiceWorker({ getSubscription: vi.fn().mockResolvedValue({ unsubscribe }) });
    await expect(unsubscribeThisDevice()).resolves.toBe(true);
    expect(unsubscribe).toHaveBeenCalledOnce();
  });
});

describe("guessDeviceLabel", () => {
  function stubUserAgent(ua: string) {
    Object.defineProperty(navigator, "userAgent", { configurable: true, value: ua });
  }

  it("recognizes Chrome on Windows", () => {
    stubUserAgent(
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    );
    expect(guessDeviceLabel()).toBe("Chrome on Windows");
  });

  it("recognizes Edge on Windows (Edge UA also contains 'Chrome')", () => {
    stubUserAgent(
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36 Edg/120.0",
    );
    expect(guessDeviceLabel()).toBe("Edge on Windows");
  });

  it("recognizes Chrome on Android", () => {
    stubUserAgent("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36");
    expect(guessDeviceLabel()).toBe("Chrome on Android");
  });

  it("recognizes Safari on iOS", () => {
    stubUserAgent(
      "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile Safari/604.1",
    );
    expect(guessDeviceLabel()).toBe("Safari on iOS");
  });
});
