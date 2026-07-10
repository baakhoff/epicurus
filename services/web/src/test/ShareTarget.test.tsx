import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Share target (#493): the service worker (src/sw.ts) stashes the OS share sheet's payload in
// the Cache API and redirects to /?share=1; ChatScreen picks it up. jsdom has no Cache Storage
// API, so it's stubbed with a small in-memory fake mirroring the real interface this code uses
// (open/match/put/delete) — good enough to drive the consuming side without a real service
// worker, which is exercised separately by the production-build check (see the PR).

const mockUpload = vi.fn();

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      models: vi.fn().mockResolvedValue([]),
      providers: vi.fn().mockResolvedValue([]),
      sessions: vi.fn().mockResolvedValue([]),
      sessionMessages: vi.fn().mockResolvedValue([]),
      suggestions: vi.fn().mockResolvedValue([]),
      modules: vi.fn().mockResolvedValue([]),
      deleteSession: vi.fn().mockResolvedValue({ deleted: 0 }),
      activeRun: vi.fn().mockResolvedValue(null),
      cancelActiveRun: vi.fn().mockResolvedValue({ cancelled: false }),
      llmPrefs: vi.fn().mockResolvedValue({
        global_default: null,
        global_embed_default: null,
        global_context_window: null,
        hidden: [],
      }),
      uploadAttachment: (file: File) => mockUpload(file),
    },
  };
});

import { ChatScreen } from "@/screens/ChatScreen";
import { SHARE_FILE_KEY, SHARE_FILE_NAME_HEADER, SHARE_META_KEY } from "@/lib/shareTarget";
import { useChat } from "@/stores/chat";

/** A minimal in-memory Cache Storage stand-in — only the surface src/sw.ts and ChatScreen
 *  actually use (open/match/put/delete on one named cache). */
function fakeCaches() {
  const entries = new Map<string, Response>();
  return {
    open: async () => ({
      match: async (key: string) => entries.get(key),
      put: async (key: string, response: Response) => {
        entries.set(key, response);
      },
      delete: async (key: string) => entries.delete(key),
    }),
    entries, // exposed for direct seeding/inspection in tests
  };
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname + location.search}</div>;
}

function renderAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <LocationProbe />
        <ChatScreen />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockUpload.mockReset();
  useChat.setState({
    sessionId: "current",
    draft: "",
    streaming: false,
    segments: [],
    pendingUser: null,
    pendingAttachments: [],
    readiness: null,
    error: null,
    paused: false,
    abort: null,
  });
});
afterEach(() => vi.unstubAllGlobals());

describe("Share target (#493)", () => {
  it("prefills the composer with the shared text and url, then clears the payload and the URL", async () => {
    const fake = fakeCaches();
    fake.entries.set(
      SHARE_META_KEY,
      new Response(JSON.stringify({ title: "A page", text: "check this out", url: "https://example.com", hasFile: false })),
    );
    vi.stubGlobal("caches", fake);

    renderAt("/?share=1");

    const textarea = (await screen.findByLabelText("Message")) as HTMLTextAreaElement;
    await waitFor(() => expect(textarea.value).toBe("check this out\nhttps://example.com"));
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toBe("/"),
    );
    expect(fake.entries.has(SHARE_META_KEY)).toBe(false); // consumed
    expect(mockUpload).not.toHaveBeenCalled();
  });

  it("uploads the shared file through the existing attachment path", async () => {
    mockUpload.mockResolvedValue({ att_id: "s1", kind: "image", title: "photo.jpg" });
    const fake = fakeCaches();
    fake.entries.set(
      SHARE_META_KEY,
      new Response(JSON.stringify({ title: "", text: "", url: "", hasFile: true })),
    );
    fake.entries.set(
      SHARE_FILE_KEY,
      // A raw byte body (not a Blob) so undici's Response ctor doesn't call Blob.stream() —
      // jsdom's Blob doesn't implement it, and the Response ctor consumes a Blob body eagerly.
      // Content-Type is set explicitly (the consumer reads the file's type from it via blob.type),
      // mirroring how src/sw.ts tags the shared file's Response.
      new Response(new TextEncoder().encode("bytes"), {
        headers: {
          "Content-Type": "image/jpeg",
          [SHARE_FILE_NAME_HEADER]: encodeURIComponent("photo.jpg"),
        },
      }),
    );
    vi.stubGlobal("caches", fake);

    renderAt("/?share=1");

    expect(await screen.findByLabelText("Remove photo.jpg")).toBeInTheDocument();
    expect(mockUpload).toHaveBeenCalledTimes(1);
    const uploaded = mockUpload.mock.calls[0][0] as File;
    expect(uploaded.name).toBe("photo.jpg");
    expect(uploaded.type).toBe("image/jpeg");
    await waitFor(() => expect(fake.entries.has(SHARE_FILE_KEY)).toBe(false));
  });

  it("appends shared text to a draft already in progress rather than clobbering it", async () => {
    useChat.setState({ draft: "existing note" });
    const fake = fakeCaches();
    fake.entries.set(
      SHARE_META_KEY,
      new Response(JSON.stringify({ title: "", text: "shared bit", url: "", hasFile: false })),
    );
    vi.stubGlobal("caches", fake);

    renderAt("/?share=1");

    const textarea = (await screen.findByLabelText("Message")) as HTMLTextAreaElement;
    await waitFor(() => expect(textarea.value).toBe("existing note\nshared bit"));
  });

  it("does nothing without the ?share=1 deep-link", async () => {
    const fake = fakeCaches();
    vi.stubGlobal("caches", fake);
    const openSpy = vi.spyOn(fake, "open");

    renderAt("/");
    await screen.findByLabelText("Message");

    expect(openSpy).not.toHaveBeenCalled();
    expect(mockUpload).not.toHaveBeenCalled();
  });

  it("still strips ?share=1 when the payload is already gone (stale/duplicate deep-link)", async () => {
    vi.stubGlobal("caches", fakeCaches());

    renderAt("/?share=1");

    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/"));
    expect(mockUpload).not.toHaveBeenCalled();
  });
});
