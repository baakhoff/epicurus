import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { type ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";

import { MailboxView } from "@/components/archetypes/MailboxView";

const mockModulePage = vi.fn();
const mockInvoke = vi.fn();
const mockSend = vi.fn();
const mockAttachmentUrl = vi.fn();
const mockMarkRead = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    modulePage: (...args: unknown[]) => mockModulePage(...args),
    invokeModuleTool: (...args: unknown[]) => mockInvoke(...args),
    sendMailboxMessage: (...args: unknown[]) => mockSend(...args),
    mailboxAttachmentUrl: (...args: unknown[]) => mockAttachmentUrl(...args),
    markMailboxThreadRead: (...args: unknown[]) => mockMarkRead(...args),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // A router, because the disconnected empty state (#764) routes the operator straight to the
  // two switches that fix it. In the app the view always mounts inside the shell's router;
  // MemoryRouter is that context without a URL bar (the Panel tests' pattern).
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

const LIST = {
  title: "Mail",
  labels: [
    { id: "INBOX", title: "Inbox", kind: "system", unread: 2 },
    { id: "SENT", title: "Sent", kind: "system" },
  ],
  active_label: "INBOX",
  query: "",
  threads: [
    {
      id: "t1",
      subject: "Project kickoff",
      sender: "alice@example.com",
      snippet: "Let's get started",
      date: "Mon, 1 Jan 2024 10:00:00 +0000",
      unread: true,
      message_count: 3,
    },
  ],
  next_cursor: null,
};

const THREAD = {
  thread: {
    id: "t1",
    subject: "Project kickoff",
    messages: [
      {
        subject: "Project kickoff",
        from: "alice@example.com",
        date: "Mon, 1 Jan 2024",
        body: "Let's get the project started next week.",
        module: "mail",
        message_id: "m1",
        unread: false,
        actions: [
          { tool: "mail_mark_unread", label: "Mark as unread", icon: "mail", args: { message_id: "m1" } },
          { tool: "mail_archive", label: "Archive", icon: "archive", args: { message_id: "m1" } },
        ],
        attachments: [{ id: "att1", filename: "agenda.pdf", mime_type: "application/pdf", size: 2048 }],
      },
    ],
    reply: {
      reply_to_message_id: "m1",
      to: "alice@example.com",
      subject: "Re: Project kickoff",
      reply_to_original: "alice@example.com — Project kickoff",
    },
  },
};

function pageImpl(_m: string, _p: string, params?: Record<string, string>) {
  if (params?.thread_id) return Promise.resolve(THREAD);
  return Promise.resolve(LIST);
}

beforeEach(() => {
  vi.clearAllMocks();
  mockModulePage.mockImplementation(pageImpl);
  mockSend.mockResolvedValue({ id: "sent-1" });
  mockInvoke.mockResolvedValue({ result: "ok" });
  mockAttachmentUrl.mockReturnValue("/platform/v1/modules/mail/pages/mailbox/attachment?x=1");
  mockMarkRead.mockResolvedValue({ thread_id: "t1", marked: 1 });
});

it("renders the labels rail and a thread row", async () => {
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  expect(await screen.findByText("Project kickoff")).toBeInTheDocument();
  // The rail shows the folder and its unread count.
  expect(screen.getAllByText("Inbox").length).toBeGreaterThan(0);
  expect(screen.getByText("2")).toBeInTheDocument(); // Inbox unread badge
  expect(screen.getByText("alice@example.com")).toBeInTheDocument();
});

it("reconciles the landing in the background and swaps in fresh data (#623)", async () => {
  // The plain landing view paints from the cache, then a second read with reconcile=1 pulls
  // the provider delta; a message that arrived after the cached read appears without a refresh.
  const RECONCILED = {
    ...LIST,
    threads: [
      { ...LIST.threads[0], id: "t2", subject: "Just arrived", sender: "bob@example.com" },
      ...LIST.threads,
    ],
  };
  mockModulePage.mockImplementation(
    (_m: string, _p: string, params?: Record<string, string>) => {
      if (params?.thread_id) return Promise.resolve(THREAD);
      if (params?.reconcile) return Promise.resolve(RECONCILED);
      return Promise.resolve(LIST);
    },
  );
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  // The cached row paints first…
  expect(await screen.findByText("Project kickoff")).toBeInTheDocument();
  // …then the reconcile read runs and its newly-arrived thread appears.
  expect(await screen.findByText("Just arrived")).toBeInTheDocument();
  await waitFor(() =>
    expect(mockModulePage).toHaveBeenCalledWith("mail", "mailbox", { reconcile: "1" }),
  );
});

it("keeps the previous folder's list visible while the next one loads, never a blank flash (#712)", async () => {
  const SENT = {
    ...LIST,
    active_label: "SENT",
    threads: [{ ...LIST.threads[0], id: "s1", subject: "Re: budget", sender: "carol@example.com" }],
  };
  let resolveSent: (value: unknown) => void = () => {};
  const sentPromise = new Promise((resolve) => {
    resolveSent = resolve;
  });
  mockModulePage.mockImplementation((_m: string, _p: string, params?: Record<string, string>) => {
    if (params?.thread_id) return Promise.resolve(THREAD);
    if (params?.label === "SENT") return sentPromise;
    return Promise.resolve(LIST);
  });
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  expect(await screen.findByText("Project kickoff")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Sent" }));

  // The Inbox row is still on screen — the folder switch never unmounts to a blank
  // spinner — while Sent's fetch is still in flight.
  expect(screen.getByText("Project kickoff")).toBeInTheDocument();
  expect(screen.queryByText("carol@example.com")).not.toBeInTheDocument();

  resolveSent(SENT);
  expect(await screen.findByText("carol@example.com")).toBeInTheDocument();
  expect(screen.queryByText("Project kickoff")).not.toBeInTheDocument();
});

it("still serialises the reconcile read behind a new folder's own read (#712, #623)", async () => {
  // The gate above this one is `listQuery.isSuccess`, and keeping the previous folder on screen
  // flips that to `true` on the first render of a folder nobody has visited — the placeholder
  // counts as success. Without excluding placeholder data the reconcile fires alongside the
  // folder's own read, i.e. two provider round-trips per first visit, which is precisely what
  // ADR-0096's gate exists to prevent.
  const SENT = {
    ...LIST,
    active_label: "SENT",
    threads: [{ ...LIST.threads[0], id: "s1", subject: "Re: budget", sender: "carol@example.com" }],
  };
  let resolveSentList: (value: unknown) => void = () => {};
  const sentList = new Promise((resolve) => {
    resolveSentList = resolve;
  });
  mockModulePage.mockImplementation((_m: string, _p: string, params?: Record<string, string>) => {
    if (params?.thread_id) return Promise.resolve(THREAD);
    if (params?.label === "SENT") return params?.reconcile ? Promise.resolve(SENT) : sentList;
    return Promise.resolve(LIST);
  });
  const sentReconciles = () =>
    mockModulePage.mock.calls.filter(
      (call) => call[2]?.reconcile === "1" && call[2]?.label === "SENT",
    );

  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  expect(await screen.findByText("Project kickoff")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Sent" }));

  // Sent's own read is in flight and Inbox's rows are still painted (that is #712 working) —
  // so the reconcile must not have started yet.
  await waitFor(() =>
    expect(mockModulePage).toHaveBeenCalledWith("mail", "mailbox", { label: "SENT" }),
  );
  expect(screen.getByText("Project kickoff")).toBeInTheDocument();
  expect(sentReconciles()).toHaveLength(0);

  resolveSentList(SENT);
  expect(await screen.findByText("carol@example.com")).toBeInTheDocument();
  // Once the folder's real data lands the gate opens, exactly once.
  await waitFor(() => expect(sentReconciles()).toHaveLength(1));
});

it("marks a thread's unread messages read on open (#625)", async () => {
  const UNREAD_THREAD = {
    thread: {
      id: "t1",
      subject: "Project kickoff",
      messages: [{ ...THREAD.thread.messages[0], message_id: "m1", unread: true }],
      reply: null,
    },
  };
  mockModulePage.mockImplementation(
    (_m: string, _p: string, params?: Record<string, string>) => {
      if (params?.thread_id) return Promise.resolve(UNREAD_THREAD);
      return Promise.resolve(LIST);
    },
  );
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  fireEvent.click(await screen.findByText("Project kickoff")); // open the thread
  // Opening it wires to the mark-read seam with the thread's unread message ids (background).
  await waitFor(() =>
    expect(mockMarkRead).toHaveBeenCalledWith("mail", "mailbox", {
      thread_id: "t1",
      message_ids: ["m1"],
    }),
  );
});

it("opens a thread and renders its message + attachment", async () => {
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  fireEvent.click(await screen.findByText("Project kickoff"));
  expect(await screen.findByText(/get the project started/)).toBeInTheDocument();
  // The thread read is fetched with the thread_id param.
  await waitFor(() =>
    expect(mockModulePage).toHaveBeenCalledWith("mail", "mailbox", { thread_id: "t1" }),
  );
  // The attachment renders as a download link built from the core-proxy URL.
  const link = await screen.findByText("agenda.pdf");
  expect(mockAttachmentUrl).toHaveBeenCalledWith("mail", "mailbox", "m1", "att1");
  expect(link.closest("a")).toHaveAttribute("download", "agenda.pdf");
});

it("renders message actions with accessible names (icon-only on mobile keeps aria-labels, #626)", async () => {
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  fireEvent.click(await screen.findByText("Project kickoff"));
  // The action buttons are addressable by their label even when the text is visually hidden on
  // a narrow viewport — the aria-label/tooltip keep them named.
  expect(await screen.findByRole("button", { name: "Mark as unread" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Archive" })).toBeInTheDocument();
});

it("searches via the module page with a q param", async () => {
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  await screen.findByText("Project kickoff");
  const box = screen.getByPlaceholderText("Search mail…");
  fireEvent.change(box, { target: { value: "invoice" } });
  fireEvent.keyDown(box, { key: "Enter" });
  await waitFor(() =>
    expect(mockModulePage).toHaveBeenCalledWith("mail", "mailbox", { q: "invoice" }),
  );
});

it("composes a new message through the send proxy (with a Send confirm)", async () => {
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  await screen.findByText("Project kickoff");
  fireEvent.click(screen.getByRole("button", { name: /New message/ }));

  fireEvent.change(await screen.findByPlaceholderText("To"), {
    target: { value: "bob@example.com" },
  });
  fireEvent.change(screen.getByPlaceholderText("Subject"), { target: { value: "Hi Bob" } });
  fireEvent.change(screen.getByPlaceholderText("Write your message…"), {
    target: { value: "Hello there" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  // The danger-action confirm gates the actual send (ADR-0087).
  const dialog = await screen.findByRole("alertdialog");
  fireEvent.click(within(dialog).getByRole("button", { name: "Send" }));

  await waitFor(() =>
    expect(mockSend).toHaveBeenCalledWith("mail", "mailbox", {
      body: "Hello there",
      to: "bob@example.com",
      subject: "Hi Bob",
    }),
  );
});

it("replies through the send proxy with the server-derived reply id", async () => {
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  fireEvent.click(await screen.findByText("Project kickoff"));
  fireEvent.click(await screen.findByRole("button", { name: /Reply/ }));

  fireEvent.change(await screen.findByPlaceholderText("Write your message…"), {
    target: { value: "Sounds good" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  const dialog = await screen.findByRole("alertdialog");
  fireEvent.click(within(dialog).getByRole("button", { name: "Send" }));

  await waitFor(() =>
    expect(mockSend).toHaveBeenCalledWith("mail", "mailbox", {
      body: "Sounds good",
      reply_to_message_id: "m1",
    }),
  );
});

it("invokes a message action (archive) through the tool proxy", async () => {
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  fireEvent.click(await screen.findByText("Project kickoff"));
  fireEvent.click(await screen.findByRole("button", { name: "Archive" }));
  await waitFor(() =>
    expect(mockInvoke).toHaveBeenCalledWith("mail", "mail_archive", { message_id: "m1" }),
  );
});

/* ── inbox category tabs (#765) ─────────────────────────────────────────── */

const TABS = [
  { id: "primary", title: "Primary", unread: 2, preview: { from: "alice@example.com", subject: "Lunch?" } },
  {
    id: "promotions",
    title: "Promotions",
    unread: 41,
    preview: { from: "deals@shop.example", subject: "50% off everything" },
  },
  { id: "forums", title: "Forums", unread: null, preview: null },
];
const TABBED = { ...LIST, tabs: TABS, active_tab: "" };

/** A page impl that serves the tabbed list, scoping the rows when `?tab=` is set. */
function tabbedPageImpl(_m: string, _p: string, params?: Record<string, string>) {
  if (params?.thread_id) return Promise.resolve(THREAD);
  if (params?.tab)
    return Promise.resolve({
      ...TABBED,
      active_tab: params.tab,
      threads: [{ ...LIST.threads[0], id: "p1", subject: `Only in ${params.tab}` }],
    });
  return Promise.resolve(TABBED);
}

it("renders the tab strip with unread badges and newest-message previews", async () => {
  mockModulePage.mockImplementation(tabbedPageImpl);
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });

  const strip = await screen.findByRole("tablist", { name: "Inbox categories" });
  expect(within(strip).getAllByRole("tab").map((t) => t.textContent)).toHaveLength(3);
  expect(within(strip).getByText("Promotions")).toBeInTheDocument();
  expect(within(strip).getByText("41")).toBeInTheDocument(); // unread badge
  expect(within(strip).getByText("deals@shop.example — 50% off everything")).toBeInTheDocument();
  // A category with no count renders no badge at all (capability gate, not a zero).
  expect(within(strip).queryByText("0")).not.toBeInTheDocument();
});

it("renders exactly today's page when the payload carries no tabs", async () => {
  // LIST has no `tabs` key at all — the pre-#765 shape, which must still render unchanged.
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  expect(await screen.findByText("Project kickoff")).toBeInTheDocument();
  expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
});

it("filters the thread list through the module when a tab is selected", async () => {
  mockModulePage.mockImplementation(tabbedPageImpl);
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  fireEvent.click(await screen.findByRole("tab", { name: /Promotions/ }));

  expect(await screen.findByText("Only in promotions")).toBeInTheDocument();
  await waitFor(() =>
    expect(mockModulePage).toHaveBeenCalledWith("mail", "mailbox", { tab: "promotions" }),
  );
  expect(screen.getByRole("tab", { name: /Promotions/ })).toHaveAttribute("aria-selected", "true");
});

it("does not fire the cache reconcile read for a tab-scoped list (#623)", async () => {
  // The module's local cache only materializes the *unscoped* landing page, so a tab read is
  // already live — a reconcile alongside it would be a second, pointless provider round-trip.
  mockModulePage.mockImplementation(tabbedPageImpl);
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  // The unscoped landing does reconcile — so the absence below is the tab, not a dead gate.
  await waitFor(() =>
    expect(mockModulePage).toHaveBeenCalledWith("mail", "mailbox", { reconcile: "1" }),
  );

  fireEvent.click(await screen.findByRole("tab", { name: /Promotions/ }));
  await screen.findByText("Only in promotions");

  const reconciles = mockModulePage.mock.calls.filter((c) => c[2]?.reconcile === "1");
  expect(reconciles.length).toBeGreaterThan(0);
  expect(reconciles.every((c) => !c[2]?.tab)).toBe(true);
});

it("clicking the active tab clears the filter and returns to the whole Inbox", async () => {
  mockModulePage.mockImplementation(tabbedPageImpl);
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  const promotions = await screen.findByRole("tab", { name: /Promotions/ });

  fireEvent.click(promotions);
  expect(await screen.findByText("Only in promotions")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("tab", { name: /Promotions/ }));

  expect(await screen.findByText("Project kickoff")).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: /Promotions/ })).toHaveAttribute(
    "aria-selected",
    "false",
  );
});

it("drops the tab selection when the folder changes", async () => {
  mockModulePage.mockImplementation((_m: string, _p: string, params?: Record<string, string>) => {
    if (params?.label === "SENT") return Promise.resolve({ ...LIST, active_label: "SENT" });
    return tabbedPageImpl(_m, _p, params);
  });
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  fireEvent.click(await screen.findByRole("tab", { name: /Promotions/ }));
  await screen.findByText("Only in promotions");

  fireEvent.click(screen.getByRole("button", { name: "Sent" }));

  // Sent is fetched with no tab param, and the strip is gone (Sent carries no tabs).
  await waitFor(() =>
    expect(mockModulePage).toHaveBeenCalledWith("mail", "mailbox", { label: "SENT" }),
  );
  await waitFor(() => expect(screen.queryByRole("tablist")).not.toBeInTheDocument());
});

it("drops the tab selection when a search is run (a search spans every folder)", async () => {
  mockModulePage.mockImplementation((_m: string, _p: string, params?: Record<string, string>) => {
    if (params?.q) return Promise.resolve({ ...LIST, query: params.q });
    return tabbedPageImpl(_m, _p, params);
  });
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  fireEvent.click(await screen.findByRole("tab", { name: /Promotions/ }));
  await screen.findByText("Only in promotions");

  const box = screen.getByPlaceholderText("Search mail…");
  fireEvent.change(box, { target: { value: "invoice" } });
  fireEvent.keyDown(box, { key: "Enter" });

  await waitFor(() =>
    expect(mockModulePage).toHaveBeenCalledWith("mail", "mailbox", { q: "invoice" }),
  );
});

it("updates the active tab's unread count after mark-read, with no full reload", async () => {
  // Opening a thread marks it read; the existing post-mark invalidation re-reads the page and
  // the module (whose category cache the mark dropped) returns the decremented count.
  let marked = false;
  const UNREAD_THREAD = {
    thread: {
      id: "t1",
      subject: "Project kickoff",
      messages: [{ ...THREAD.thread.messages[0], message_id: "m1", unread: true }],
      reply: null,
    },
  };
  mockModulePage.mockImplementation((_m: string, _p: string, params?: Record<string, string>) => {
    if (params?.thread_id) return Promise.resolve(UNREAD_THREAD);
    return Promise.resolve({
      ...TABBED,
      tabs: TABS.map((t) => (t.id === "primary" ? { ...t, unread: marked ? 1 : 2 } : t)),
    });
  });
  mockMarkRead.mockImplementation(() => {
    marked = true;
    return Promise.resolve({ thread_id: "t1", marked: 1 });
  });

  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  const strip = await screen.findByRole("tablist", { name: "Inbox categories" });
  expect(within(strip).getByText("2")).toBeInTheDocument();

  fireEvent.click(await screen.findByText("Project kickoff")); // opens + marks read
  await waitFor(() => expect(mockMarkRead).toHaveBeenCalled());
  fireEvent.click(await screen.findByRole("button", { name: "Back to list" }));

  const refreshed = await screen.findByRole("tablist", { name: "Inbox categories" });
  await waitFor(() => expect(within(refreshed).getByText("1")).toBeInTheDocument());
});

it("surfaces a thread-open error (not the silent list) with a Back control", async () => {
  // The list loads, but the thread fetch fails with a relayed Gmail hint (#538/#557).
  mockModulePage.mockImplementation((_m: string, _p: string, params?: Record<string, string>) => {
    if (params?.thread_id) {
      return Promise.reject(new Error("Gmail is rate-limiting this account"));
    }
    return Promise.resolve(LIST);
  });
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  fireEvent.click(await screen.findByText("Project kickoff"));
  // The hint is shown rather than silently falling back to the list.
  expect(await screen.findByText(/rate-limiting/)).toBeInTheDocument();
  // Back clears the failed thread id and returns to the list (a re-open would refetch).
  fireEvent.click(screen.getByRole("button", { name: /Back to list/ }));
  expect(await screen.findByText("Project kickoff")).toBeInTheDocument();
});

/* ── no Google connected (#764) ─────────────────────────────────────────────── */
// Mail is provider-only (ADR-0032), so the module answers with an empty list carrying
// `disconnected` instead of an error. The shell's job is to say why, and offer the exits.

const DISCONNECTED = {
  title: "Mail",
  labels: [],
  active_label: "INBOX",
  query: "",
  tabs: [],
  active_tab: "",
  threads: [],
  next_cursor: null,
  disconnected: true,
};

function disconnectedPage() {
  mockModulePage.mockImplementation(() => Promise.resolve(DISCONNECTED));
}

it("names the missing Google connection instead of showing an empty folder (#764)", async () => {
  disconnectedPage();
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  expect(await screen.findByText("Google is not connected.")).toBeInTheDocument();
  // Both ways out, in the copy the issue asked for.
  expect(screen.getByText(/Connect it in Settings/)).toBeInTheDocument();
  expect(screen.getByText(/disable the mail module/)).toBeInTheDocument();
  // Never the generic "this folder is empty" — that reads as "you have no mail".
  expect(screen.queryByText("This folder is empty.")).not.toBeInTheDocument();
});

it("keeps the disconnected page out of the error state (no toast, no failure copy)", async () => {
  disconnectedPage();
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  await screen.findByText("Google is not connected.");
  expect(screen.queryByText(/Couldn't reach your mail/)).not.toBeInTheDocument();
});

it("hides the folder rail, search, and compose while disconnected", async () => {
  disconnectedPage();
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  await screen.findByText("Google is not connected.");
  expect(screen.queryByRole("navigation", { name: "Mailbox folders" })).not.toBeInTheDocument();
  expect(screen.queryByPlaceholderText("Search mail…")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /New message/ })).not.toBeInTheDocument();
});

it("offers one-tap routes to the two switches that fix it", async () => {
  disconnectedPage();
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  expect(await screen.findByRole("button", { name: /Open Settings/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Modules" })).toBeInTheDocument();
});

it("renders the ordinary page when the payload omits the flag (pre-#764 modules)", async () => {
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  expect(await screen.findByText("Project kickoff")).toBeInTheDocument();
  expect(screen.queryByText("Google is not connected.")).not.toBeInTheDocument();
});

it("distinguishes an empty folder from a missing connection", async () => {
  // A connected mailbox with nothing in the folder keeps the old, correct copy.
  mockModulePage.mockImplementation(() => Promise.resolve({ ...LIST, threads: [] }));
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  expect(await screen.findByText("This folder is empty.")).toBeInTheDocument();
  expect(screen.queryByText("Google is not connected.")).not.toBeInTheDocument();
});

/* ── the core couldn't be reached (#835) ────────────────────────────────────── */
// The third state, and the one the shell used to get wrong: with `is_available()` collapsing
// every token-fetch failure to False, a core that was merely restarting produced the
// disconnected panel above — telling an operator to go reconnect an account that was fine.
// The module now sends `unreachable` with the reason instead, and this is what it must draw.

const UNREACHABLE = {
  title: "Mail",
  labels: [],
  active_label: "INBOX",
  query: "",
  tabs: [],
  active_tab: "",
  threads: [],
  next_cursor: null,
  unreachable: "couldn't reach the core to fetch the Google token: connection refused",
};

function unreachablePage() {
  mockModulePage.mockImplementation(() => Promise.resolve(UNREACHABLE));
}

it("says it couldn't check the connection, and never that Google is disconnected (#835)", async () => {
  unreachablePage();
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  expect(await screen.findByText("Couldn't check your mail connection.")).toBeInTheDocument();
  // The misdiagnosis this issue exists to remove — in either of its wordings.
  expect(screen.queryByText("Google is not connected.")).not.toBeInTheDocument();
  expect(screen.queryByText(/Connect it in Settings/)).not.toBeInTheDocument();
  // Nor the generic empty folder, which would read as "you have no mail".
  expect(screen.queryByText("This folder is empty.")).not.toBeInTheDocument();
});

it("shows the operator the actual reason", async () => {
  unreachablePage();
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  expect(await screen.findByText(/connection refused/)).toBeInTheDocument();
  expect(screen.getByText(/nothing to reconnect/)).toBeInTheDocument();
});

it("offers a retry rather than the two disconnect exits", async () => {
  unreachablePage();
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
  // Routing to Settings here is worse than useless: the account is probably fine, and the
  // only thing to do there is disconnect it.
  expect(screen.queryByRole("button", { name: /Open Settings/ })).not.toBeInTheDocument();
});

it("refetches the list when the retry is pressed", async () => {
  unreachablePage();
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  const before = mockModulePage.mock.calls.length;
  fireEvent.click(await screen.findByRole("button", { name: "Try again" }));
  await waitFor(() => expect(mockModulePage.mock.calls.length).toBeGreaterThan(before));
});

it("actually recovers the page when the retry finds a healthy core", async () => {
  // The property the copy above promises ("It should come back on its own"). On the landing
  // view the reconcile read wins over the list read, so a retry that refreshed only the list
  // would leave the *stale* unreachable payload on screen forever, however healthy the core.
  unreachablePage();
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  await screen.findByText("Couldn't check your mail connection.");

  mockModulePage.mockImplementation(pageImpl); // the core is back
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));

  expect(await screen.findByText("Project kickoff")).toBeInTheDocument();
  expect(screen.queryByText("Couldn't check your mail connection.")).not.toBeInTheDocument();
});

it("hides the folder rail, search, and compose while unreachable too", async () => {
  unreachablePage();
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  await screen.findByText("Couldn't check your mail connection.");
  expect(screen.queryByRole("navigation", { name: "Mailbox folders" })).not.toBeInTheDocument();
  expect(screen.queryByPlaceholderText("Search mail…")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /New message/ })).not.toBeInTheDocument();
});

it("keeps the unreachable page out of the error state", async () => {
  unreachablePage();
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  await screen.findByText("Couldn't check your mail connection.");
  // A 200 with an honest payload, not a failed query: plain navigation to Mail must not break.
  expect(screen.queryByText(/Couldn't reach your mail\./)).not.toBeInTheDocument();
});

it("renders the ordinary page when the payload omits the flag (pre-#835 modules)", async () => {
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  expect(await screen.findByText("Project kickoff")).toBeInTheDocument();
  expect(screen.queryByText("Couldn't check your mail connection.")).not.toBeInTheDocument();
});
