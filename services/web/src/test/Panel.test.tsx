import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { PanelHost } from "@/components/Panel";
import { usePanel } from "@/stores/panel";

const DETAIL = {
  title: "Standup",
  description: "Daily sync",
  details: [{ label: "When", value: "9:00" }],
  href: { label: "Open in calendar", url: "https://cal.example/123" },
};

beforeEach(() => usePanel.getState().close());

describe("PanelHost", () => {
  it("renders nothing when the panel is closed", () => {
    render(<PanelHost />);
    expect(screen.queryByRole("button", { name: "Close panel" })).toBeNull();
  });

  it("renders an entity-detail view opened through the store", () => {
    render(<PanelHost />);
    act(() => usePanel.getState().open("entity-detail", DETAIL, "Event"));
    // Rendered in both the desktop column and the (hidden) phone sheet.
    expect(screen.getAllByText("Standup").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Daily sync").length).toBeGreaterThan(0);
    expect(screen.getAllByText("When").length).toBeGreaterThan(0);
    expect(screen.getAllByRole("link", { name: /Open in calendar/ }).length).toBeGreaterThan(0);
  });

  it("closes when the close button is pressed", () => {
    render(<PanelHost />);
    act(() => usePanel.getState().open("entity-detail", DETAIL, "Event"));
    fireEvent.click(screen.getByRole("button", { name: "Close panel" }));
    expect(usePanel.getState().stack).toEqual([]);
  });

  it("drops an unsafe (non-http) entity-detail link", () => {
    render(<PanelHost />);
    act(() =>
      usePanel.getState().open(
        "entity-detail",
        { ...DETAIL, href: { label: "danger", url: "javascript:alert(1)" } },
        "Event",
      ),
    );
    expect(screen.queryByRole("link", { name: "danger" })).toBeNull();
  });

  it("renders the email-reader view", () => {
    render(<PanelHost />);
    act(() =>
      usePanel
        .getState()
        .open("email-reader", { subject: "Lunch?", from: "a@b.com", body: "noon" }, "Email"),
    );
    expect(screen.getAllByText("Lunch?").length).toBeGreaterThan(0);
    expect(screen.getAllByText("noon").length).toBeGreaterThan(0);
  });
});
