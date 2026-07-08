import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PanelHost } from "@/components/Panel";
import { useChat } from "@/stores/chat";
import { useConnection } from "@/stores/connection";
import { usePanel } from "@/stores/panel";

const DRAFT = {
  to: "bob@x.com",
  cc: null,
  subject: "Lunch?",
  body: "Noon works.",
  reply_to_original: null,
};

function renderPanel() {
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <PanelHost />
    </QueryClientProvider>,
  );
}

function openDraft(draft: object = DRAFT) {
  act(() => usePanel.getState().open("email-draft", draft, "Review email"));
}

beforeEach(() => {
  usePanel.getState().close();
  useConnection.setState({ online: true, coreDown: false });
  useChat.setState({ streaming: false, sessionId: "s1", resolveDraft: vi.fn(async () => {}) });
});

describe("email-draft panel (ADR-0085, #563)", () => {
  it("renders the composed draft with Confirm & Decline", () => {
    renderPanel();
    openDraft();
    expect(screen.getAllByText("Lunch?").length).toBeGreaterThan(0);
    expect(screen.getAllByText("bob@x.com").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Noon works.").length).toBeGreaterThan(0);
    expect(screen.getAllByRole("button", { name: /Confirm/ }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("button", { name: /Decline/ }).length).toBeGreaterThan(0);
  });

  it("shows the reply thread context when present", () => {
    renderPanel();
    openDraft({ ...DRAFT, reply_to_original: "alice@x.com — Re: Lunch" });
    expect(screen.getAllByText(/Replying to alice@x.com/).length).toBeGreaterThan(0);
  });

  it("Confirm calls resolveDraft('send')", () => {
    const resolveDraft = vi.fn(async () => {});
    useChat.setState({ resolveDraft });
    renderPanel();
    openDraft();
    fireEvent.click(screen.getAllByRole("button", { name: /Confirm/ })[0]);
    expect(resolveDraft).toHaveBeenCalledWith("send", expect.any(Function));
  });

  it("Decline calls resolveDraft('decline')", () => {
    const resolveDraft = vi.fn(async () => {});
    useChat.setState({ resolveDraft });
    renderPanel();
    openDraft();
    fireEvent.click(screen.getAllByRole("button", { name: /Decline/ })[0]);
    expect(resolveDraft).toHaveBeenCalledWith("decline", expect.any(Function));
  });

  it("disables Confirm while the connection is lost (#530) but keeps Decline available", () => {
    useConnection.setState({ coreDown: true });
    renderPanel();
    openDraft();
    expect(screen.getAllByRole("button", { name: /Confirm/ })[0]).toBeDisabled();
    // Decline stays available — the operator can always back out.
    expect(screen.getAllByRole("button", { name: /Decline/ })[0]).not.toBeDisabled();
    expect(screen.getAllByText(/can't send right now/).length).toBeGreaterThan(0);
  });

  it("Escape declines the draft (the destructive path is never the default)", () => {
    const resolveDraft = vi.fn(async () => {});
    useChat.setState({ resolveDraft });
    renderPanel();
    openDraft();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(resolveDraft).toHaveBeenCalledWith("decline", expect.any(Function));
  });
});
