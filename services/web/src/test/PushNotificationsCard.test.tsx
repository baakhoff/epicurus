import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PushNotificationsCard } from "@/components/PushNotificationsCard";
import type { PushDeviceRecord, PushPrefs } from "@/lib/contracts";

const mockSubscriptions = vi.fn();
const mockCreateSubscription = vi.fn();
const mockDeleteSubscription = vi.fn();
const mockPrefs = vi.fn();
const mockSetPrefs = vi.fn();
const mockTestNotification = vi.fn();
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
      mail: { push: false, center: true },
    },
    known_categories: KNOWN_CATEGORIES,
    quiet_hours_enabled: false,
    quiet_hours_start: "22:00",
    quiet_hours_end: "07:00",
    ...overrides,
  };
}

beforeEach(() => {
  mockSubscriptions.mockReset().mockResolvedValue([]);
  mockCreateSubscription.mockReset().mockResolvedValue(device());
  mockDeleteSubscription.mockReset().mockResolvedValue(undefined);
  mockPrefs.mockReset().mockResolvedValue(prefs());
  mockSetPrefs.mockReset().mockResolvedValue(prefs());
  mockTestNotification.mockReset().mockResolvedValue({ outcome: "sent", sent_count: 1, pruned_count: 0 });
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

  it("toggling a category preserves its stored center value", async () => {
    render(<PushNotificationsCard />, { wrapper });
    const mailRow = (await screen.findByText("Mail")).closest("div") as HTMLElement;
    fireEvent.click(within(mailRow).getByRole("switch"));

    await waitFor(() =>
      expect(mockSetPrefs).toHaveBeenCalledWith({
        categories: { mail: { push: true, center: true } }, // flips push, keeps center
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
    mockTestNotification.mockResolvedValue({ outcome: "skipped_no_devices", sent_count: 0, pruned_count: 0 });
    render(<PushNotificationsCard />, { wrapper });

    fireEvent.click(await screen.findByRole("button", { name: /send test notification/i }));
    expect(await screen.findByText(/no devices to send it to/i)).toBeInTheDocument();
  });
});
