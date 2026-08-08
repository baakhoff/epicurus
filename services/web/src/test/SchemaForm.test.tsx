import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SchemaForm } from "@/components/SchemaForm";

describe("SchemaForm", () => {
  it("renders string, boolean and enum fields from a JSON Schema", () => {
    render(
      <SchemaForm
        schema={{
          type: "object",
          properties: {
            greeting: { type: "string", title: "Greeting", description: "shown first" },
            enabled: { type: "boolean", title: "Enabled" },
            mode: { type: "string", title: "Mode", enum: ["calm", "eager"] },
          },
        }}
        onSubmit={() => {}}
      />,
    );
    expect(screen.getByText("Greeting")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: /Enabled/ })).toBeInTheDocument();
    expect(screen.getByRole("combobox")).toBeInTheDocument();
  });

  it("submits typed values and drops empty optional fields", () => {
    const onSubmit = vi.fn();
    render(
      <SchemaForm
        schema={{
          type: "object",
          properties: {
            name: { type: "string", title: "Name" },
            count: { type: "integer", title: "Count" },
            empty: { type: "string", title: "Empty" },
          },
          required: ["name"],
        }}
        onSubmit={onSubmit}
      />,
    );
    fireEvent.change(screen.getByRole("textbox", { name: "Name *" }), {
      target: { value: "sam" },
    });
    fireEvent.change(screen.getByRole("spinbutton", { name: "Count" }), {
      target: { value: "3" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onSubmit).toHaveBeenCalledWith({ name: "sam", count: 3 });
  });

  it("disables submit while a required field is empty", () => {
    render(
      <SchemaForm
        schema={{
          type: "object",
          properties: { key: { type: "string", title: "Key" } },
          required: ["key"],
        }}
        onSubmit={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("prefills defaults and stored values", () => {
    render(
      <SchemaForm
        schema={{
          type: "object",
          properties: {
            a: { type: "string", title: "A", default: "from-default" },
            b: { type: "string", title: "B" },
          },
        }}
        initial={{ b: "from-store" }}
        onSubmit={() => {}}
      />,
    );
    expect(screen.getByDisplayValue("from-default")).toBeInTheDocument();
    expect(screen.getByDisplayValue("from-store")).toBeInTheDocument();
  });

  it("renders a plain run button for an empty schema (no-arg actions)", () => {
    const onSubmit = vi.fn();
    render(<SchemaForm schema={{}} submitLabel="Run" onSubmit={onSubmit} />);
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(onSubmit).toHaveBeenCalledWith({});
  });

  it("resolves an optional (anyOf) field to its real type (#208)", () => {
    // Python `str | None` arrives as anyOf; the enum member must still render a select.
    render(
      <SchemaForm
        schema={{
          type: "object",
          properties: {
            mode: { anyOf: [{ type: "string", enum: ["a", "b"] }, { type: "null" }], title: "Mode" },
          },
        }}
        onSubmit={() => {}}
      />,
    );
    expect(screen.getByRole("combobox")).toBeInTheDocument();
  });

  it("renders enumLabels as a labeled <select> (label≠value) and submits the value (#253)", () => {
    // A list picker: options show the list title (enumLabels) but submit the list id (enum).
    const onSubmit = vi.fn();
    render(
      <SchemaForm
        schema={{
          type: "object",
          properties: {
            list_id: {
              type: "string",
              title: "List",
              enum: ["id-job", "id-life"],
              enumLabels: ["Job", "Life"],
            },
          },
        }}
        onSubmit={onSubmit}
      />,
    );
    // The shown option labels are the titles; their values are the ids.
    expect(screen.getByRole("option", { name: "Job" })).toHaveValue("id-job");
    expect(screen.getByRole("option", { name: "Life" })).toHaveValue("id-life");
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "id-life" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onSubmit).toHaveBeenCalledWith({ list_id: "id-life" });
  });

  it("renders a date-time field as a picker and submits an ISO instant (#208)", () => {
    const onSubmit = vi.fn();
    render(
      <SchemaForm
        schema={{
          type: "object",
          properties: { start: { type: "string", format: "date-time", title: "Start" } },
        }}
        onSubmit={onSubmit}
      />,
    );
    const input = screen.getByLabelText("Start");
    expect(input).toHaveAttribute("type", "datetime-local");
    fireEvent.change(input, { target: { value: "2026-06-20T10:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    const submitted = onSubmit.mock.calls[0][0].start as string;
    // Stored as an ISO-8601 instant; parsing it back yields the chosen local wall time.
    expect(new Date(submitted).getTime()).toBe(new Date("2026-06-20T10:00").getTime());
  });

  it("renders a labeled select, showing the label but submitting the value (field_choices)", () => {
    const onSubmit = vi.fn();
    render(
      <SchemaForm
        schema={{
          type: "object",
          properties: {
            calendar_id: {
              type: "string",
              title: "Calendar",
              enum: ["local", "google:primary"],
              enumLabels: ["Local", "Personal"],
            },
          },
        }}
        initial={{ calendar_id: "google:primary" }}
        onSubmit={onSubmit}
      />,
    );
    // The human-friendly label is shown; the submitted value is the opaque token.
    expect(screen.getByRole("option", { name: "Personal" })).toHaveValue("google:primary");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onSubmit.mock.calls[0][0].calendar_id).toBe("google:primary");
  });

  it("collapses a date-time field to a date picker when its all-day toggle is on", () => {
    const onSubmit = vi.fn();
    render(
      <SchemaForm
        schema={{
          type: "object",
          properties: {
            all_day: { type: "boolean", title: "All day" },
            start: { type: "string", format: "date-time", date_toggle: "all_day", title: "Start" },
          },
        }}
        initial={{ all_day: true, start: "2026-06-15" }}
        onSubmit={onSubmit}
      />,
    );
    const input = screen.getByLabelText("Start");
    expect(input).toHaveAttribute("type", "date");
    fireEvent.change(input, { target: { value: "2026-06-20" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    // A floating date string, never an ISO instant — so the shell can't timezone-shift it.
    expect(onSubmit.mock.calls[0][0].start).toBe("2026-06-20");
  });

  it("switches the field back to datetime when the all-day toggle is turned off", () => {
    render(
      <SchemaForm
        schema={{
          type: "object",
          properties: {
            all_day: { type: "boolean", title: "All day" },
            start: { type: "string", format: "date-time", date_toggle: "all_day", title: "Start" },
          },
        }}
        initial={{ all_day: true, start: "2026-06-15" }}
        onSubmit={() => {}}
      />,
    );
    expect(screen.getByLabelText("Start")).toHaveAttribute("type", "date");
    fireEvent.click(screen.getByRole("switch", { name: /All day/ }));
    expect(screen.getByLabelText("Start")).toHaveAttribute("type", "datetime-local");
  });

  it("constrains inputs and the submit so the form fits a narrow mobile sheet (#335)", () => {
    render(
      <SchemaForm
        schema={{
          type: "object",
          properties: {
            start: { type: "string", format: "date-time", title: "Start" },
            mode: { type: "string", title: "Mode", enum: ["a", "b"] },
          },
        }}
        onSubmit={() => {}}
      />,
    );
    // `min-w-0` lets a native date picker shrink to the sheet instead of overflowing it.
    expect(screen.getByLabelText("Start").className).toContain("min-w-0");
    expect(screen.getByRole("combobox").className).toContain("min-w-0");
    // The submit reads as a full-width action bar at the foot of the sheet.
    expect(screen.getByRole("button", { name: "Save" }).className).toContain("w-full");
  });

  // ── repeat picker (format: rrule, #471) ──────────────────────────────────────

  const rruleSchema = {
    type: "object",
    properties: { repeat: { type: "string", format: "rrule", title: "Repeat" } },
  } as const;

  it("renders the repeat picker and submits a preset's canonical RRULE (#471)", () => {
    const onSubmit = vi.fn();
    render(<SchemaForm schema={rruleSchema} onSubmit={onSubmit} />);
    fireEvent.change(screen.getByLabelText("Repeat"), { target: { value: "weekly" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onSubmit).toHaveBeenCalledWith({ repeat: "FREQ=WEEKLY" });
  });

  it("preselects the matching preset when editing an existing rule", () => {
    render(<SchemaForm schema={rruleSchema} initial={{ repeat: "FREQ=DAILY" }} onSubmit={() => {}} />);
    expect(screen.getByLabelText("Repeat")).toHaveValue("daily");
  });

  it("reveals a raw RRULE input for Custom and submits it verbatim", () => {
    const onSubmit = vi.fn();
    render(<SchemaForm schema={rruleSchema} onSubmit={onSubmit} />);
    fireEvent.change(screen.getByLabelText("Repeat"), { target: { value: "custom" } });
    fireEvent.change(screen.getByLabelText("Repeat custom rule"), {
      target: { value: "FREQ=MONTHLY;INTERVAL=2" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onSubmit).toHaveBeenCalledWith({ repeat: "FREQ=MONTHLY;INTERVAL=2" });
  });

  it("submits nothing for 'Does not repeat' (a one-off task)", () => {
    const onSubmit = vi.fn();
    render(<SchemaForm schema={rruleSchema} onSubmit={onSubmit} />);
    // The default selection is "none" → an empty rule, dropped from the submitted args.
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onSubmit).toHaveBeenCalledWith({});
  });

  it("sends an explicit clear when 'Does not repeat' replaces an existing rule (#515)", () => {
    const onSubmit = vi.fn();
    render(<SchemaForm schema={rruleSchema} initial={{ repeat: "FREQ=WEEKLY" }} onSubmit={onSubmit} />);
    fireEvent.change(screen.getByLabelText("Repeat"), { target: { value: "none" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    // Previously this silently dropped `repeat` (indistinguishable from "leave it alone"), so
    // an existing rule could never be cleared from the board form.
    expect(onSubmit).toHaveBeenCalledWith({ repeat: "" });
  });

  it("sends an explicit clear for any optional field blanked after having a value, not just repeat", () => {
    const onSubmit = vi.fn();
    render(
      <SchemaForm
        schema={{ type: "object", properties: { notes: { type: "string", title: "Notes" } } }}
        initial={{ notes: "buy milk" }}
        onSubmit={onSubmit}
      />,
    );
    fireEvent.change(screen.getByRole("textbox", { name: "Notes" }), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onSubmit).toHaveBeenCalledWith({ notes: "" });
  });

  // ── the `format: "tags"` chips input (#763) ─────────────────────────────────

  const tagsSchema = (suggestions?: string[]) => ({
    type: "object" as const,
    properties: {
      tags: { type: "string", title: "Tags", format: "tags", suggestions },
    },
  });

  it("renders an existing tags value as removable chips inside the box (#763)", () => {
    const onSubmit = vi.fn();
    render(
      <SchemaForm schema={tagsSchema()} initial={{ tags: "work, q3" }} onSubmit={onSubmit} />,
    );
    expect(screen.getByText("work")).toBeInTheDocument();
    expect(screen.getByText("q3")).toBeInTheDocument();
    // Removing a chip drops just that tag from the serialized value.
    fireEvent.click(screen.getByRole("button", { name: "Remove tag work" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onSubmit).toHaveBeenCalledWith({ tags: "q3" });
  });

  it("commits a typed tag on Enter and keeps the comma-separated serialization", () => {
    const onSubmit = vi.fn();
    render(
      <SchemaForm schema={tagsSchema()} initial={{ tags: "work" }} onSubmit={onSubmit} />,
    );
    const input = screen.getByRole("textbox", { name: "Tags" });
    fireEvent.change(input, { target: { value: "urgent" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByText("urgent")).toBeInTheDocument(); // now a chip
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    // The tool's contract is unchanged: one comma-separated string.
    expect(onSubmit).toHaveBeenCalledWith({ tags: "work, urgent" });
  });

  it("commits on comma and dedupes case-insensitively", () => {
    const onSubmit = vi.fn();
    render(<SchemaForm schema={tagsSchema()} onSubmit={onSubmit} />);
    const input = screen.getByRole("textbox", { name: "Tags" });
    fireEvent.change(input, { target: { value: "home," } });
    expect(screen.getByText("home")).toBeInTheDocument();
    // A duplicate (any casing) is not added twice.
    fireEvent.change(input, { target: { value: "Home," } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onSubmit).toHaveBeenCalledWith({ tags: "home" });
  });

  it("offers module-supplied suggestions as a typeahead and adds one on pick", () => {
    const onSubmit = vi.fn();
    render(
      <SchemaForm
        schema={tagsSchema(["errand", "work", "q3"])}
        initial={{ tags: "work" }}
        onSubmit={onSubmit}
      />,
    );
    const input = screen.getByRole("textbox", { name: "Tags" });
    fireEvent.focus(input);
    const menu = screen.getByRole("listbox", { name: "Tags suggestions" });
    // Already-chosen tags aren't re-suggested.
    expect(menu).toHaveTextContent("errand");
    expect(menu).toHaveTextContent("q3");
    expect(menu).not.toHaveTextContent("work");
    // Typing narrows the matches.
    fireEvent.change(input, { target: { value: "err" } });
    expect(screen.getByRole("listbox", { name: "Tags suggestions" })).not.toHaveTextContent("q3");
    fireEvent.mouseDown(screen.getByRole("option", { name: "errand" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onSubmit).toHaveBeenCalledWith({ tags: "work, errand" });
  });

  it("creates a brand-new tag inline — suggestions are offered, never enforced", () => {
    const onSubmit = vi.fn();
    render(<SchemaForm schema={tagsSchema(["errand"])} onSubmit={onSubmit} />);
    const input = screen.getByRole("textbox", { name: "Tags" });
    fireEvent.change(input, { target: { value: "brand-new" } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onSubmit).toHaveBeenCalledWith({ tags: "brand-new" });
  });

  it("keeps the suggestions overlay through an anyOf (optional param) collapse", () => {
    // A module tool's optional `tags: str | None` arrives as anyOf; the action's
    // field_suggestions land on the outer prop — resolveProp must not drop them.
    render(
      <SchemaForm
        schema={{
          type: "object",
          properties: {
            tags: {
              anyOf: [{ type: "string", format: "tags" }, { type: "null" }],
              title: "Tags",
              suggestions: ["errand"],
            },
          },
        }}
        onSubmit={() => {}}
      />,
    );
    fireEvent.focus(screen.getByRole("textbox", { name: "Tags" }));
    expect(screen.getByRole("option", { name: "errand" })).toBeInTheDocument();
  });
});
