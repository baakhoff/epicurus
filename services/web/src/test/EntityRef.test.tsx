import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  EntityRefChip,
  EntityRefsContext,
  SmartLink,
  SourcesPill,
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

  // #572: an unnamed `group-hover` matches ANY ancestor `.group`, not just the chip's own
  // wrapper — so nesting the chip inside another group (a message row, a sessions list row)
  // reveals the card on that ancestor's hover too. Naming the scope is the fix; pin the classes.
  it("scopes the hover-card reveal to its own named group, not an unnamed ancestor group (#572)", () => {
    render(<EntityRefChip entref={REF} />, { wrapper });
    const chipWrapper = screen.getByRole("button", { name: /Standup/ }).parentElement!;
    const wrapperClasses = chipWrapper.className.split(/\s+/);
    expect(wrapperClasses).toContain("group/chip");
    expect(wrapperClasses).not.toContain("group");

    const card = chipWrapper.lastElementChild as HTMLElement;
    const cardClasses = card.className.split(/\s+/);
    expect(cardClasses).toEqual(
      expect.arrayContaining([
        "group-hover/chip:visible",
        "group-hover/chip:opacity-100",
        "group-focus-within/chip:visible",
        "group-focus-within/chip:opacity-100",
      ]),
    );
    // Regression pin: no unnamed group-* variant survives, which would match any ancestor group.
    expect(cardClasses).not.toContain("group-hover:visible");
    expect(cardClasses).not.toContain("group-hover:opacity-100");
    expect(cardClasses).not.toContain("group-focus-within:visible");
    expect(cardClasses).not.toContain("group-focus-within:opacity-100");
  });
});

describe("SourcesPill (#333)", () => {
  const REFS: EntityRef[] = [
    REF,
    { ref_id: "e2", module: "knowledge", kind: "doc", title: "Roadmap", summary: null },
  ];

  it("collapses the refs behind one pill, expanding to the chips on click", () => {
    render(<SourcesPill refs={REFS} />, { wrapper });
    // Collapsed: a single "Sources (N)" pill — no per-source chips on screen yet.
    expect(screen.getByRole("button", { name: /Sources \(2\)/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Standup/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Sources \(2\)/ }));
    expect(screen.getByRole("button", { name: /Standup/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Roadmap/ })).toBeInTheDocument();
  });

  it("renders nothing when there are no refs", () => {
    const { container } = render(<SourcesPill refs={[]} />, { wrapper });
    expect(container).toBeEmptyDOMElement();
  });

  // #572: before the named-group fix, hovering the expanded pill (or any chip in the row)
  // satisfied every chip's `group-hover`, fanning out one absolutely-positioned card per
  // source, stacked over each other. Each chip must own exactly one card, in its own scope.
  it("gives each chip exactly one hover-card container, scoped to its own group, when expanded (#572)", () => {
    const { container } = render(<SourcesPill refs={REFS} />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: /Sources \(2\)/ }));

    const chipWrappers = Array.from(container.getElementsByClassName("group/chip"));
    expect(chipWrappers).toHaveLength(REFS.length);
    for (const chip of chipWrappers) {
      expect(chip.getElementsByClassName("group-hover/chip:visible")).toHaveLength(1);
    }
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
