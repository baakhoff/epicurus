import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PushNotificationsCard } from "@/components/PushNotificationsCard";
import type { PushDeviceRecord, PushPrefs, PushStatus } from "@/lib/contracts";

const mockSubscriptions = vi.fn();
const mockCreateSubscription = vi.fn();
const mockDeleteSubscription = vi.fn();
const mockPrefs = vi.fn();
const mockSetPrefs = vi.fn();
const mockTestNotification = vi.fn();
const mockStatus = vi.fn();
const mockEventSubscriptions = vi.fn();
vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    detail: string;
    constructor(status: number, detail: string) {
      super(detail);
      this.status = status;
      this.detail = detail;
    }
  },
  api: {
    pushSubscriptions: (...a: unknown[]) => mockSubscriptions(...a),
    createPushSubscription: (...a: unknown[]) => mockCreateSubscription(...a),
    deletePushSubscription: (...a: unknown[]) => mockDeleteSubscription(...a),
    pushPrefs: (...a: unknown[]) => mockPrefs(...a),
    setPushPrefs: (...a: unknown[]) => mockSetPrefs(...a),
    sendTestPushNotification: (...a: unknown[]) => mockTestNotification(...a),
    pushStatus: (...a: unknown[]) => mockStatus(...a),
    eventSubscriptions: (...a: unknown[]) => mockEventSubscriptions(...a),
  },
}));

const mockIsSupported = vi.fn();
const mockGetExisting = vi.fn();
const mockSubscribeDevice = vi.fn();
const mockUnsubscribeDevice = vi.fn();
const mockGuessLabel = vi.fn();
vi.mock("@/lib/push", () => ({
  isPushSupported: (...a: unknown[]) => mockIsSupported(...a),
  getExistingSubscription: (...a: unknown[]) => mockGetExisting(...a),
  subscribeThisDevice: (...a: unknown[]) => mockSubscribeDevice(...a),
  unsubscribeThisDevice: (...a: unknown[]) => mockUnsubscribeDevice(...a),
  guessDeviceLabel: (...a: unknown[]) => mockGuessLabel(...a),
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function device(overrides: Partial<PushDeviceRecord> = {}): PushDeviceRecord {
  return {
    id: "d1",
    device_label: "Chrome on Windows",
    created_at: "2026-07-01T00:00:00Z",
    last_seen_at: null,
    ...overrides,
  };
}

const KNOWN_CATEGORIES = [
  { id: "system", label: "System" },
  { id: "chat", label: "Chat & agent" },
  { id: "mail", label: "Mail" },
];

function prefs(overrides: Partial<PushPrefs> = {}): PushPrefs {
  return {
    categories: {
      system: { push: true, center: true },
      chat: { push: true, center: true },
      // A pre-#797 stored value: push off and, unusually, center off. The card must read
      // only the push half and converge center to true on the next write.
      mail: { push: false, center: false },
    },
    known_categories: KNOWN_CATEGORIES,
    quiet_hours_enabled: false,
    quiet_hours_start: "22:00",
    quiet_hours_end: "07:00",
    ...overrides,
  };
}

function status(overrides: Partial<PushStatus> = {}): PushStatus {
  return { device_count: 1, last_attempt: null, ...overrides };
}

function attempt(
  overrides: Partial<NonNullable<PushStatus["last_attempt"]>> = {},
): NonNullable<PushStatus["last_attempt"]> {
  return {
    at: "2026-08-08T10:00:00Z",
    category: "system",
    outcome: "sent",
    sent_count: 1,
    failed_count: 0,
    pruned_count: 0,
    ...overrides,
  };
}

beforeEach(() => {
  mockSubscriptions.mockReset().mockResolvedValue([]);
  mockCreateSubscription.mockReset().mockResolvedValue(device());
  mockDeleteSubscription.mockReset().mockResolvedValue(undefined);
  mockPrefs.mockReset().mockResolvedValue(prefs());
  mockSetPrefs.mockReset().mockResolvedValue(prefs());
  mockTestNotification.mockReset().mockResolvedValue({
    outcome: "sent",
    sent_count: 1,
    pruned_count: 0,
    failed_count: 0,
  });
  mockStatus.mockReset().mockResolvedValue(status());
  mockEventSubscriptions.mockReset().mockResolvedValue([]);
  mockIsSupported.mockReset().mockReturnValue(true);
  mockGetExisting.mockReset().mockResolvedValue(null);
  mockSubscribeDevice.mockReset();
  mockUnsubscribeDevice.mockReset().mockResolvedValue(true);
  mockGuessLabel.mockReset().mockReturnValue("Chrome on Windows");
});

describe("PushNotificationsCard (#670, ADR-0102)", () => {
  it("shows an unsupported message and no subscribe control when push isn't supported", async () => {
    mockIsSupported.mockReturnValue(false);
    render(<PushNotificationsCard />, { wrapper });
    expect(await screen.findByText(/doesn't support push notifications/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^subscribe$/i })).not.toBeInTheDocument();
  });

  it("shows Subscribe when this device has no existing subscription", async () => {
    mockGetExisting.mockResolvedValue(null);
    render(<PushNotificationsCard />, { wrapper });
    expect(await screen.findByText(/this device is not subscribed/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^subscribe$/i })).toBeInTheDocument();
  });

  it("shows Unsubscribe when this device is already subscribed", async () => {
    mockGetExisting.mockResolvedValue({ endpoint: "https://push.example/abc" });
    render(<PushNotificationsCard />, { wrapper });
    expect(await screen.findByText(/this device is subscribed/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^unsubscribe$/i })).toBeInTheDocument();
  });

  it("subscribing posts the browser subscription's endpoint and keys to the backend", async () => {
    mockGetExisting.mockResolvedValue(null);
    mockSubscribeDevice.mockResolvedValue({
      toJSON: () => ({ endpoint: "https://push.example/new", keys: { p256dh: "p", auth: "a" } }),
    });
    render(<PushNotificationsCard />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: /^subscribe$/i }));

    await waitFor(() =>
      expect(mockCreateSubscription).toHaveBeenCalledWith({
        endpoint: "https://push.example/new",
        p256dh: "p",
        auth: "a",
        device_label: "Chrome on Windows",
      }),
    );
  });

  it("shows an error when subscribing is denied rather than crashing", async () => {
    mockGetExisting.mockResolvedValue(null);
    mockSubscribeDevice.mockResolvedValue(null); // permission denied / unsupported
    render(<PushNotificationsCard />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: /^subscribe$/i }));
    expect(await screen.findByText(/permission was denied/i)).toBeInTheDocument();
    expect(mockCreateSubscription).not.toHaveBeenCalled();
  });

  it("unsubscribing calls unsubscribeThisDevice", async () => {
    mockGetExisting.mockResolvedValue({ endpoint: "https://push.example/abc" });
    render(<PushNotificationsCard />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: /^unsubscribe$/i }));
    await waitFor(() => expect(mockUnsubscribeDevice).toHaveBeenCalledOnce());
  });

  it("shows an empty state when no devices are subscribed", async () => {
    render(<PushNotificationsCard />, { wrapper });
    expect(await screen.findByText(/no devices subscribed yet/i)).toBeInTheDocument();
  });

  it("lists subscribed devices and deletes one on request", async () => {
    mockSubscriptions.mockResolvedValue([device({ id: "d1", device_label: "My Phone" })]);
    render(<PushNotificationsCard />, { wrapper });

    expect(await screen.findByText("My Phone")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /remove my phone/i }));
    await waitFor(() => expect(mockDeleteSubscription).toHaveBeenCalledWith("d1"));
  });

  it("renders one toggle per known category, reflecting its stored push value", async () => {
    render(<PushNotificationsCard />, { wrapper });
    const mailRow = (await screen.findByText("Mail")).closest("div") as HTMLElement;
    const mailSwitch = within(mailRow).getByRole("switch");
    expect(mailSwitch).toHaveAttribute("aria-checked", "false"); // mail.push is false in fixture

    const systemRow = screen.getByText("System").closest("div") as HTMLElement;
    expect(within(systemRow).getByRole("switch")).toHaveAttribute("aria-checked", "true");
  });

  it("toggling a category always writes center: true, converging stored pre-#797 values", async () => {
    render(<PushNotificationsCard />, { wrapper });
    const mailRow = (await screen.findByText("Mail")).closest("div") as HTMLElement;
    fireEvent.click(within(mailRow).getByRole("switch"));

    await waitFor(() =>
      expect(mockSetPrefs).toHaveBeenCalledWith({
        // The fixture stores mail as center: false; the write converges it to true — the
        // center records everything since #797, so false is never written back.
        categories: { mail: { push: true, center: true } },
      }),
    );
  });

  it("toggling quiet hours on/off calls setPushPrefs immediately", async () => {
    render(<PushNotificationsCard />, { wrapper });
    fireEvent.click(await screen.findByRole("switch", { name: /enable quiet hours/i }));
    await waitFor(() =>
      expect(mockSetPrefs).toHaveBeenCalledWith({ quiet_hours_enabled: true }),
    );
  });

  it("editing quiet-hours times reveals Save, and Save persists both times", async () => {
    render(<PushNotificationsCard />, { wrapper });
    await screen.findByLabelText(/^from$/i);
    expect(screen.queryByRole("button", { name: /^save$/i })).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/^from$/i), { target: { value: "23:00" } });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() =>
      expect(mockSetPrefs).toHaveBeenCalledWith({
        quiet_hours_enabled: true,
        quiet_hours_start: "23:00",
        quiet_hours_end: "07:00",
      }),
    );
  });

  it("sends a test notification and shows the delivered outcome", async () => {
    mockTestNotification.mockResolvedValue({ outcome: "sent", sent_count: 2, pruned_count: 0 });
    render(<PushNotificationsCard />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: /send test notification/i }));

    await waitFor(() => expect(mockTestNotification).toHaveBeenCalledWith("system"));
    expect(await screen.findByText(/sent to 2 device/i)).toBeInTheDocument();
  });

  it("explains the no-devices outcome in plain language", async () => {
    mockTestNotification.mockResolvedValue({
      outcome: "skipped_no_devices",
      sent_count: 0,
      pruned_count: 0,
      failed_count: 0,
    });
    render(<PushNotificationsCard />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: /send test notification/i }));
    expect(await screen.findByText(/no devices to send it to/i)).toBeInTheDocument();
  });

  it("calls out a test send that failed on every device (#797)", async () => {
    mockTestNotification.mockResolvedValue({
      outcome: "sent",
      sent_count: 0,
      pruned_count: 0,
      failed_count: 2,
    });
    render(<PushNotificationsCard />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: /send test notification/i }));
    expect(await screen.findByText(/delivery failed on every device/i)).toBeInTheDocument();
  });

  // ── delivery state + the no-device warning (#797) ──────────────────────────────

  it("warns when push is enabled but no device is registered", async () => {
    mockStatus.mockResolvedValue(status({ device_count: 0 }));
    render(<PushNotificationsCard />, { wrapper });
    expect(
      await screen.findByText(/push is turned on, but no device is registered/i),
    ).toBeInTheDocument();
  });

  it("does not warn when a device is registered", async () => {
    mockStatus.mockResolvedValue(status({ device_count: 1 }));
    render(<PushNotificationsCard />, { wrapper });
    await screen.findByText(/no push attempted/i); // the status query has resolved
    expect(
      screen.queryByText(/push is turned on, but no device is registered/i),
    ).not.toBeInTheDocument();
  });

  it("does not warn when nothing has push enabled anywhere", async () => {
    mockStatus.mockResolvedValue(status({ device_count: 0 }));
    mockPrefs.mockResolvedValue(
      prefs({
        categories: {
          system: { push: false, center: true },
          chat: { push: false, center: true },
          mail: { push: false, center: true },
        },
      }),
    );
    mockEventSubscriptions.mockResolvedValue([]);
    render(<PushNotificationsCard />, { wrapper });
    await screen.findByText(/no push attempted/i); // status resolved, prefs render follows
    await screen.findByText("Mail"); // prefs resolved too — the warning had its inputs
    expect(
      screen.queryByText(/push is turned on, but no device is registered/i),
    ).not.toBeInTheDocument();
  });

  it("warns on a push-enabled event alert even with every category off", async () => {
    mockStatus.mockResolvedValue(status({ device_count: 0 }));
    mockPrefs.mockResolvedValue(
      prefs({
        categories: {
          system: { push: false, center: true },
          chat: { push: false, center: true },
          mail: { push: false, center: true },
        },
      }),
    );
    mockEventSubscriptions.mockResolvedValue([
      { module: "mail", event_type: "mail.received", push: true, center: true },
    ]);
    render(<PushNotificationsCard />, { wrapper });
    expect(
      await screen.findByText(/push is turned on, but no device is registered/i),
    ).toBeInTheDocument();
  });

  it("explains that nothing has been attempted since startup when status is empty", async () => {
    mockStatus.mockResolvedValue(status({ last_attempt: null }));
    render(<PushNotificationsCard />, { wrapper });
    expect(await screen.findByText(/no push attempted since the server last started/i)).toBeInTheDocument();
  });

  it("shows the last delivery attempt with its outcome", async () => {
    mockStatus.mockResolvedValue(
      status({ last_attempt: attempt({ outcome: "sent", sent_count: 2 }) }),
    );
    render(<PushNotificationsCard />, { wrapper });
    expect(await screen.findByText(/last push attempt/i)).toBeInTheDocument();
    expect(screen.getByText(/delivered to 2 device/i)).toBeInTheDocument();
  });

  it("reads an all-devices-failed attempt as a failure, not a success", async () => {
    mockStatus.mockResolvedValue(
      status({
        device_count: 1,
        last_attempt: attempt({ outcome: "sent", sent_count: 0, failed_count: 1 }),
      }),
    );
    render(<PushNotificationsCard />, { wrapper });
    expect(await screen.findByText(/failed — 0 of 1 device/i)).toBeInTheDocument();
  });

  it("describes a quiet-hours-held attempt", async () => {
    mockStatus.mockResolvedValue(status({ last_attempt: attempt({ outcome: "queued" }) }));
    render(<PushNotificationsCard />, { wrapper });
    expect(await screen.findByText(/held for the quiet-hours digest/i)).toBeInTheDocument();
  });

  it("refreshes the delivery status after a test send", async () => {
    render(<PushNotificationsCard />, { wrapper });
    await screen.findByText(/no push attempted/i);
    expect(mockStatus).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: /send test notification/i }));
    await waitFor(() => expect(mockStatus).toHaveBeenCalledTimes(2)); // invalidated on settle
  });
});
