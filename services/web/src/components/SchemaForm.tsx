/**
 * SchemaForm — renders a constrained JSON-Schema subset as a native-looking
 * form. One renderer powers module config forms AND tool/action argument
 * forms (the same vocabulary as MCP tool inputs, per ADR-0007).
 *
 * Guaranteed subset (v1): root `type: object`; properties of type string
 * (enum → select, format "multiline" → textarea), number/integer, boolean.
 * Honored keywords: title, description, default, required, enum, minimum,
 * maximum. Anything else degrades to a raw JSON field — never a crash.
 */
import { useMemo, useState } from "react";

import { Button, Label, Select, Switch, TextArea, TextInput } from "@/components/ui";
import { REPEAT_PRESETS, presetForRule, type RepeatPresetId } from "@/lib/rrule";

interface PropertySchema {
  type?: string;
  title?: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
  // Display labels parallel to ``enum`` (a labeled <select>); falls back to the value.
  enumLabels?: string[];
  format?: string;
  // Names a sibling boolean field that, when true, renders this date-time field as a
  // *date* picker emitting a ``YYYY-MM-DD`` value (the calendar's all-day toggle).
  date_toggle?: string;
  // Open typeahead values for a ``format: "tags"`` chips field (#763) — offered, never
  // enforced (unlike ``enum``). Overlaid from a board action's ``field_suggestions``.
  suggestions?: string[];
  minimum?: number;
  maximum?: number;
  // An optional field (e.g. Python ``str | None``) arrives as ``anyOf`` of the real
  // member plus ``{type: "null"}``; ``resolveProp`` collapses it to the real member.
  anyOf?: PropertySchema[];
}

/** A bare floating date, ``YYYY-MM-DD``. */
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

export interface ObjectSchema {
  type?: string;
  properties?: Record<string, PropertySchema>;
  required?: string[];
}

export type FormValues = Record<string, unknown>;

/** Collapse an ``anyOf`` (optional/union) prop to its first non-null member, keeping
 *  the outer title/description/default — and any overlaid ``suggestions`` (#763), which
 *  a board action attaches at the top level — so the field renders by its real type +
 *  format without losing the overlay. */
function resolveProp(prop: PropertySchema): PropertySchema {
  if (!prop.anyOf || prop.anyOf.length === 0) return prop;
  const member = prop.anyOf.find((p) => p.type && p.type !== "null") ?? prop.anyOf[0];
  return {
    ...member,
    title: prop.title ?? member.title,
    description: prop.description ?? member.description,
    default: prop.default ?? member.default,
    suggestions: prop.suggestions ?? member.suggestions,
  };
}

/** ISO-8601 (with offset) → a ``datetime-local`` input value in the browser's zone. */
function toLocalInput(value: unknown): string {
  if (typeof value !== "string" || !value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** A ``date`` input value (``YYYY-MM-DD``) from either a floating date or an ISO datetime. */
function toDateInput(value: unknown): string {
  if (typeof value !== "string" || !value) return "";
  if (DATE_ONLY.test(value)) return value; // already a floating date — never shift it
  const local = toLocalInput(value); // ISO datetime → local wall time → take the date part
  return local ? local.slice(0, 10) : "";
}

/** A ``datetime-local`` value (browser-zone wall time) → ISO-8601 UTC for the tool. */
function fromLocalInput(local: string): string {
  const d = new Date(local);
  return Number.isNaN(d.getTime()) ? local : d.toISOString();
}

function initialValues(schema: ObjectSchema, current: FormValues): FormValues {
  const values: FormValues = {};
  for (const [key, raw] of Object.entries(schema.properties ?? {})) {
    const prop = resolveProp(raw);
    if (current[key] !== undefined) values[key] = current[key];
    else if (prop.default !== undefined && prop.default !== null) values[key] = prop.default;
    else if (prop.type === "boolean") values[key] = false;
    else values[key] = "";
  }
  return values;
}

/**
 * The shared repeat picker (#471) for a `format: "rrule"` field: a friendly dropdown
 * (None / Daily / Weekdays / Weekly / Monthly / Yearly / Custom) that emits a bare RRULE
 * string. Choosing a named preset writes its canonical rule; "Custom…" reveals a raw RRULE
 * input for anything the presets don't cover. Used by both the tasks and calendar forms.
 */
function RepeatField({
  title,
  description,
  value,
  onChange,
}: {
  title: string;
  description?: string;
  value: unknown;
  onChange: (next: unknown) => void;
}) {
  const rule = typeof value === "string" ? value : "";
  // The dropdown selection is UI state seeded from the incoming rule (so editing opens on the
  // right option). While "Custom…" is chosen we keep the box open regardless of what's typed,
  // so a half-typed rule that momentarily matches a preset doesn't collapse the input.
  const [preset, setPreset] = useState<RepeatPresetId>(() => presetForRule(rule));

  const choose = (id: RepeatPresetId) => {
    setPreset(id);
    if (id === "custom") return; // keep the current rule for the user to edit; don't clear it
    onChange(REPEAT_PRESETS.find((p) => p.id === id)!.rrule);
  };

  return (
    <div>
      <Label hint={description}>{title}</Label>
      <Select
        aria-label={title}
        value={preset}
        onChange={(e) => choose(e.target.value as RepeatPresetId)}
        className="w-full"
      >
        {REPEAT_PRESETS.map((p) => (
          <option key={p.id} value={p.id}>
            {p.label}
          </option>
        ))}
      </Select>
      {preset === "custom" && (
        <TextInput
          aria-label={`${title} custom rule`}
          className="mt-2 font-mono text-xs"
          placeholder="FREQ=WEEKLY;INTERVAL=2"
          value={rule}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </div>
  );
}

/** Split a comma-separated tags value into its clean tag list. */
function splitTags(value: unknown): string[] {
  if (typeof value !== "string" || !value) return [];
  return value
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
}

/**
 * The chips input for a `format: "tags"` field (#763): the current tags as removable
 * chips **inside** the input box ("a box inside a box"), a typeahead over the
 * module-supplied `suggestions` (offered, never enforced — a new tag is created by
 * typing it), and Enter/comma committing the entry. The field's *value* stays the
 * comma-separated string the tool already accepts, so the MCP contract is unchanged.
 */
function TagsField({
  title,
  description,
  suggestions,
  value,
  onChange,
}: {
  title: string;
  description?: string;
  suggestions?: string[];
  value: unknown;
  onChange: (next: unknown) => void;
}) {
  const tags = splitTags(value);
  const [entry, setEntry] = useState("");
  const [focused, setFocused] = useState(false);

  const commit = (raw: string) => {
    const tag = raw.trim();
    setEntry("");
    if (!tag || tags.some((t) => t.toLocaleLowerCase() === tag.toLocaleLowerCase())) return;
    onChange([...tags, tag].join(", "));
  };
  const remove = (tag: string) => onChange(tags.filter((t) => t !== tag).join(", "));

  // Typeahead: known tags not already chosen, filtered by the entry (all while empty —
  // the picker doubles as "what tags exist?"), capped to keep the menu scannable.
  const matches = (suggestions ?? [])
    .filter((s) => !tags.some((t) => t.toLocaleLowerCase() === s.toLocaleLowerCase()))
    .filter((s) => s.toLocaleLowerCase().includes(entry.trim().toLocaleLowerCase()))
    .slice(0, 8);

  return (
    <div>
      <Label hint={description}>{title}</Label>
      <div
        className={cnField(focused)}
        onMouseDown={(e) => {
          // Clicking the box focuses the inner input (unless a chip button was hit).
          if (e.target === e.currentTarget) {
            e.preventDefault();
            (e.currentTarget.querySelector("input") as HTMLInputElement | null)?.focus();
          }
        }}
      >
        {tags.map((tag) => (
          <span
            key={tag}
            className="inline-flex items-center gap-1 rounded-(--radius-field) bg-accent-dim px-1.5 py-0.5 text-xs text-accent-strong"
          >
            {tag}
            <button
              type="button"
              aria-label={`Remove tag ${tag}`}
              onClick={() => remove(tag)}
              className="leading-none opacity-70 transition-opacity hover:opacity-100"
            >
              ×
            </button>
          </span>
        ))}
        {/* eslint-disable-next-line no-restricted-syntax -- the chips *container* is the
            themed field (#763, "a box inside a box"): TextInput's own border/background
            would draw a second box around this inner entry, which is deliberately bare. */}
        <input
          aria-label={title}
          value={entry}
          placeholder={tags.length === 0 ? "Add a tag…" : ""}
          onChange={(e) => {
            // A pasted/typed comma commits everything before it, chip by chip.
            const raw = e.target.value;
            if (raw.includes(",")) {
              const parts = raw.split(",");
              for (const part of parts.slice(0, -1)) commit(part);
              setEntry(parts[parts.length - 1]);
            } else {
              setEntry(raw);
            }
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              // Commit the pending entry instead of submitting the form — but let a bare
              // Enter (nothing pending) fall through to the form's submit.
              if (entry.trim()) {
                e.preventDefault();
                commit(entry);
              }
            } else if (e.key === "Backspace" && entry === "" && tags.length > 0) {
              remove(tags[tags.length - 1]); // erase backwards into the chips
            }
          }}
          onFocus={() => setFocused(true)}
          onBlur={() => {
            setFocused(false);
            commit(entry); // don't lose a half-typed tag on blur/submit-click
          }}
          className="min-w-24 flex-1 bg-transparent text-sm text-ink outline-none placeholder:text-ink-faint"
        />
      </div>
      {focused && matches.length > 0 && (
        <div className="mt-1 flex flex-wrap gap-1" role="listbox" aria-label={`${title} suggestions`}>
          {matches.map((s) => (
            <button
              key={s}
              type="button"
              role="option"
              aria-selected={false}
              // onMouseDown so the pick lands before the input's blur commits the entry.
              onMouseDown={(e) => {
                e.preventDefault();
                commit(s);
              }}
              className="rounded-(--radius-field) border border-edge px-1.5 py-0.5 text-xs text-ink-dim transition-colors hover:border-accent hover:text-accent-strong"
            >
              {s}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** The chips container's classes — a bordered "box" the tag "boxes" sit inside. */
function cnField(focused: boolean): string {
  return [
    "flex min-h-9 w-full cursor-text flex-wrap items-center gap-1 rounded-(--radius-field)",
    "border bg-surface px-2 py-1.5 transition-colors",
    focused ? "border-accent" : "border-edge",
  ].join(" ");
}

function FieldFor({
  name,
  prop,
  required,
  value,
  values,
  onChange,
}: {
  name: string;
  prop: PropertySchema;
  required: boolean;
  value: unknown;
  /** All current form values — so a field can react to a sibling (e.g. the all-day toggle). */
  values: FormValues;
  onChange: (next: unknown) => void;
}) {
  const title = (prop.title ?? name) + (required ? " *" : "");

  if (prop.type === "boolean") {
    return (
      <div className="flex items-center justify-between gap-4">
        <Label hint={prop.description}>{title}</Label>
        <Switch checked={Boolean(value)} onChange={onChange} label={title} />
      </div>
    );
  }

  if (prop.enum && prop.enum.length > 0) {
    const labels = prop.enumLabels;
    return (
      <div>
        <Label hint={prop.description}>{title}</Label>
        <Select
          aria-label={title}
          value={String(value ?? "")}
          onChange={(e) => onChange(e.target.value)}
          className="w-full"
        >
          <option value="" disabled>
            choose…
          </option>
          {prop.enum.map((option, i) => (
            <option key={String(option)} value={String(option)}>
              {labels?.[i] ?? String(option)}
            </option>
          ))}
        </Select>
      </div>
    );
  }

  if (prop.format === "rrule") {
    return <RepeatField title={title} description={prop.description} value={value} onChange={onChange} />;
  }

  if (prop.format === "tags") {
    return (
      <TagsField
        title={title}
        description={prop.description}
        suggestions={prop.suggestions}
        value={value}
        onChange={onChange}
      />
    );
  }

  if (prop.format === "date-time" || prop.format === "date") {
    // A date-time field collapses to a date picker when its `date_toggle` sibling is on
    // (the all-day toggle) or when it is a plain `date` field; it then emits a floating
    // `YYYY-MM-DD` value, which the shell never timezone-shifts.
    const asDate = prop.format === "date" || (!!prop.date_toggle && Boolean(values[prop.date_toggle]));
    return (
      <div>
        <Label hint={prop.description}>{title}</Label>
        <TextInput
          type={asDate ? "date" : "datetime-local"}
          aria-label={title}
          value={asDate ? toDateInput(value) : toLocalInput(value)}
          onChange={(e) => {
            const raw = e.target.value;
            if (raw === "") return onChange("");
            onChange(asDate ? raw : fromLocalInput(raw));
          }}
        />
      </div>
    );
  }

  if (prop.type === "number" || prop.type === "integer") {
    return (
      <div>
        <Label hint={prop.description}>{title}</Label>
        <TextInput
          type="number"
          inputMode="decimal"
          aria-label={title}
          min={prop.minimum}
          max={prop.maximum}
          step={prop.type === "integer" ? 1 : "any"}
          value={value === "" || value == null ? "" : String(value)}
          onChange={(e) => {
            const raw = e.target.value;
            if (raw === "") return onChange("");
            onChange(prop.type === "integer" ? parseInt(raw, 10) : parseFloat(raw));
          }}
        />
      </div>
    );
  }

  if (prop.type === "string" || prop.type === undefined) {
    const multiline = prop.format === "multiline";
    return (
      <div>
        <Label hint={prop.description}>{title}</Label>
        {multiline ? (
          <TextArea
            rows={3}
            aria-label={title}
            value={String(value ?? "")}
            onChange={(e) => onChange(e.target.value)}
          />
        ) : (
          <TextInput
            aria-label={title}
            value={String(value ?? "")}
            onChange={(e) => onChange(e.target.value)}
          />
        )}
      </div>
    );
  }

  // Unsupported property type — degrade honestly to raw JSON, never crash.
  return (
    <div>
      <Label hint={`unsupported field type "${prop.type}" — raw JSON`}>{title}</Label>
      <TextArea
        rows={3}
        aria-label={title}
        className="font-mono text-xs"
        value={typeof value === "string" ? value : JSON.stringify(value ?? null, null, 2)}
        onChange={(e) => {
          try {
            onChange(JSON.parse(e.target.value));
          } catch {
            onChange(e.target.value);
          }
        }}
      />
    </div>
  );
}

export function SchemaForm({
  schema,
  initial = {},
  submitLabel = "Save",
  busy = false,
  onSubmit,
}: {
  schema: ObjectSchema;
  initial?: FormValues;
  submitLabel?: string;
  busy?: boolean;
  onSubmit: (values: FormValues) => void;
}) {
  const start = useMemo(() => initialValues(schema, initial), [schema, initial]);
  const [values, setValues] = useState<FormValues>(start);
  const properties = Object.entries(schema.properties ?? {});
  const required = new Set(schema.required ?? []);

  if (properties.length === 0) {
    return (
      <Button variant="primary" busy={busy} onClick={() => onSubmit({})}>
        {submitLabel}
      </Button>
    );
  }

  const missing = [...required].some((key) => {
    const v = values[key];
    return v === "" || v == null;
  });

  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(e) => {
        e.preventDefault();
        // An optional field left blank throughout (never had a value) is omitted — nothing to
        // change. One that *had* a value and was blanked (e.g. "Does not repeat" over an
        // existing rule) sends an explicit "" instead, so the tool can tell "clear this" apart
        // from "leave it alone" (#515 — previously always dropped, so clearing never reached
        // the tool at all).
        const cleaned: FormValues = {};
        for (const [key, value] of Object.entries(values)) {
          if (value === "" && !required.has(key)) {
            if (start[key] !== "" && start[key] != null) cleaned[key] = "";
            continue;
          }
          cleaned[key] = value;
        }
        onSubmit(cleaned);
      }}
    >
      {properties.map(([name, prop]) => (
        <FieldFor
          key={name}
          name={name}
          prop={resolveProp(prop)}
          required={required.has(name)}
          value={values[name]}
          values={values}
          onChange={(next) => setValues((prev) => ({ ...prev, [name]: next }))}
        />
      ))}
      {/* Full-width submit so the action reads as a clear bar at the foot of a narrow
          mobile sheet rather than a stray button (#335). */}
      <Button type="submit" variant="primary" className="w-full" busy={busy} disabled={missing}>
        {submitLabel}
      </Button>
    </form>
  );
}
