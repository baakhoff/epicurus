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
});
