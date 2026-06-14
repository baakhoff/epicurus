import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  EntityRefChip,
  EntityRefsContext,
  SmartLink,
  inlinedRefIds,
  refsById,
} from "@/components/EntityRef";
import type { EntityRef } from "@/lib/contracts";
import { usePanel } from "@/stores/panel";

const mockResolve = vi.fn();
vi.mock("@/lib/api", () => ({
  api: { resolveEntity: (...args: unknown[]) => mockResolve(...args) },
}));

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const REF: EntityRef = {
  ref_id: "e1",
  module: "calendar",
  kind: "event",
  title: "Standup",
  summary: "9am",
};

beforeEach(() => {
  mockResolve.mockReset();
  usePanel.getState().close();
});

describe("EntityRefChip", () => {
  it("renders the entity title as a chip", () => {
    render(<EntityRefChip entref={REF} />, { wrapper });
    expect(screen.getByRole("button", { name: /Standup/ })).toBeInTheDocument();
  });

  it("opens the entity in the right panel on click", () => {
    render(<EntityRefChip entref={REF} />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: /Standup/ }));
    expect(usePanel.getState().stack.at(-1)).toMatchObject({
      view: "entity-detail",
      title: "Standup",
    });
  });

  it("resolves the hover-card from the module resolver on hover", async () => {
    mockResolve.mockResolvedValue({ title: "Standup", description: "Daily sync", details: [] });
    render(<EntityRefChip entref={REF} />, { wrapper });
    fireEvent.mouseEnter(screen.getByRole("button", { name: /Standup/ }).parentElement!);
    await waitFor(() => expect(mockResolve).toHaveBeenCalledWith("calendar", "event", "e1"));
  });
});

describe("SmartLink", () => {
  it("renders an entity chip for the epicurus:// scheme", () => {
    render(
      <EntityRefsContext.Provider value={refsById([REF])}>
        <SmartLink href="epicurus://entity/calendar/event/e1">Standup</SmartLink>
      </EntityRefsContext.Provider>,
      { wrapper },
    );
    expect(screen.getByRole("button", { name: /Standup/ })).toBeInTheDocument();
  });

  it("renders a normal anchor for an http link", () => {
    render(<SmartLink href="https://example.com">site</SmartLink>, { wrapper });
    const link = screen.getByRole("link", { name: "site" });
    expect(link).toHaveAttribute("href", "https://example.com");
    expect(link).toHaveAttribute("target", "_blank");
  });
});

describe("inlinedRefIds", () => {
  it("extracts ref ids from entity-scheme links in text", () => {
    expect(inlinedRefIds("see [x](epicurus://entity/m/k/e1) here")).toEqual(new Set(["e1"]));
  });

  it("is empty when there are no entity links", () => {
    expect(inlinedRefIds("just prose")).toEqual(new Set());
  });
});
