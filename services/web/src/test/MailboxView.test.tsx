import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { type ReactNode } from "react";
import { beforeEach, expect, it, vi } from "vitest";

import { MailboxView } from "@/components/archetypes/MailboxView";

const mockModulePage = vi.fn();
const mockInvoke = vi.fn();
const mockSend = vi.fn();
const mockAttachmentUrl = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    modulePage: (...args: unknown[]) => mockModulePage(...args),
    invokeModuleTool: (...args: unknown[]) => mockInvoke(...args),
    sendMailboxMessage: (...args: unknown[]) => mockSend(...args),
    mailboxAttachmentUrl: (...args: unknown[]) => mockAttachmentUrl(...args),
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
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
});

it("renders the labels rail and a thread row", async () => {
  render(<MailboxView module="mail" pageId="mailbox" />, { wrapper });
  expect(await screen.findByText("Project kickoff")).toBeInTheDocument();
  // The rail shows the folder and its unread count.
  expect(screen.getAllByText("Inbox").length).toBeGreaterThan(0);
  expect(screen.getByText("2")).toBeInTheDocument(); // Inbox unread badge
  expect(screen.getByText("alice@example.com")).toBeInTheDocument();
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
