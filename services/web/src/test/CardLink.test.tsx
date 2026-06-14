import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { CardLink } from "@/components/CardLink";

describe("CardLink", () => {
  it("renders an in-app path as a same-tab router link", () => {
    render(
      <MemoryRouter>
        <CardLink href={{ label: "Open in Knowledge", url: "/m/knowledge/vault?doc=a.md" }} />
      </MemoryRouter>,
    );
    const link = screen.getByRole("link", { name: /Open in Knowledge/ });
    expect(link).toHaveAttribute("href", "/m/knowledge/vault?doc=a.md");
    expect(link).not.toHaveAttribute("target");
  });

  it("renders an external http(s) link in a new tab", () => {
    render(<CardLink href={{ label: "GitHub", url: "https://example.com/x" }} />);
    const link = screen.getByRole("link", { name: /GitHub/ });
    expect(link).toHaveAttribute("href", "https://example.com/x");
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("drops an unsafe scheme", () => {
    render(<CardLink href={{ label: "danger", url: "javascript:alert(1)" }} />);
    expect(screen.queryByRole("link")).toBeNull();
  });
});
