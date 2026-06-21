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

import { Button, Label, Switch, TextArea, TextInput } from "@/components/ui";

interface PropertySchema {
  type?: string;
  title?: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
  format?: string;
  minimum?: number;
  maximum?: number;
  // An optional field (e.g. Python ``str | None``) arrives as ``anyOf`` of the real
  // member plus ``{type: "null"}``; ``resolveProp`` collapses it to the real member.
  anyOf?: PropertySchema[];
}

export interface ObjectSchema {
  type?: string;
  properties?: Record<string, PropertySchema>;
  required?: string[];
}

export type FormValues = Record<string, unknown>;

/** Collapse an ``anyOf`` (optional/union) prop to its first non-null member, keeping
 *  the outer title/description/default so the field renders by its real type + format. */
function resolveProp(prop: PropertySchema): PropertySchema {
  if (!prop.anyOf || prop.anyOf.length === 0) return prop;
  const member = prop.anyOf.find((p) => p.type && p.type !== "null") ?? prop.anyOf[0];
  return {
    ...member,
    title: prop.title ?? member.title,
    description: prop.description ?? member.description,
    default: prop.default ?? member.default,
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

function FieldFor({
  name,
  prop,
  required,
  value,
  onChange,
}: {
  name: string;
  prop: PropertySchema;
  required: boolean;
  value: unknown;
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
    return (
      <div>
        <Label hint={prop.description}>{title}</Label>
        <select
          aria-label={title}
          value={String(value ?? "")}
          onChange={(e) => onChange(e.target.value)}
          className="w-full rounded-(--radius-field) border border-edge bg-surface-2 px-3 py-2 text-sm text-ink focus:border-accent focus:outline-none"
        >
          <option value="" disabled>
            choose…
          </option>
          {prop.enum.map((option) => {
            // An option is a plain string (label==value) or a {value,label} pair, so the
            // submitted value can differ from the shown label (e.g. a list id vs its title).
            const pair =
              typeof option === "object" && option !== null && "value" in option
                ? (option as { value: string; label: string })
                : null;
            const optValue = pair ? pair.value : String(option);
            const optLabel = pair ? pair.label : String(option);
            return (
              <option key={optValue} value={optValue}>
                {optLabel}
              </option>
            );
          })}
        </select>
      </div>
    );
  }

  if (prop.format === "date-time" || prop.format === "date") {
    const isDate = prop.format === "date";
    return (
      <div>
        <Label hint={prop.description}>{title}</Label>
        <TextInput
          type={isDate ? "date" : "datetime-local"}
          aria-label={title}
          value={isDate ? String(value ?? "") : toLocalInput(value)}
          onChange={(e) => {
            const raw = e.target.value;
            if (raw === "") return onChange("");
            onChange(isDate ? raw : fromLocalInput(raw));
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
        // Drop empty optional strings so we send only what was filled in.
        const cleaned: FormValues = {};
        for (const [key, value] of Object.entries(values)) {
          if (value === "" && !required.has(key)) continue;
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
          onChange={(next) => setValues((prev) => ({ ...prev, [name]: next }))}
        />
      ))}
      <Button type="submit" variant="primary" busy={busy} disabled={missing}>
        {submitLabel}
      </Button>
    </form>
  );
}
