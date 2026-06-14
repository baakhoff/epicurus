/**
 * Chat entity references (ADR-0019). When the assistant mentions a module entity it
 * carries a structured `EntityRef`; the UI renders it as a chip — hover shows a core
 * hover-card (enriched on demand from the module's resolver), click opens it in the
 * right panel. The hover-card / panel shapes are core-owned; modules supply only data.
 */
import { useQuery } from "@tanstack/react-query";
import { AtSign } from "lucide-react";
import { createContext, useContext, useState, type ReactNode } from "react";

import { CardLink } from "@/components/CardLink";
import { api } from "@/lib/api";
import type { EntityRef, HoverCard } from "@/lib/contracts";
import { usePanel } from "@/stores/panel";

/** Markdown links with this scheme render as entity chips instead of anchors. */
const ENTITY_LINK_RE = /^epicurus:\/\/entity\/([^/]+)\/([^/]+)\/(.+)$/;
const ENTITY_LINK_GLOBAL = /epicurus:\/\/entity\/[^/]+\/[^/]+\/([^)\s]+)/g;

/** The entity refs of the message currently being rendered, keyed by ref_id. */
export const EntityRefsContext = createContext<Map<string, EntityRef>>(new Map());

/** Build the lookup map a message provides to its markdown. */
export function refsById(refs: EntityRef[]): Map<string, EntityRef> {
  return new Map(refs.map((ref) => [ref.ref_id, ref]));
}

/** The ref_ids referenced inline in `text` (so the chip row can skip them). */
export function inlinedRefIds(text: string): Set<string> {
  const ids = new Set<string>();
  for (const match of text.matchAll(ENTITY_LINK_GLOBAL)) ids.add(decodeURIComponent(match[1]));
  return ids;
}

function cardFromRef(ref: EntityRef): HoverCard {
  return { title: ref.title, description: ref.summary ?? "", details: [] };
}

function HoverCardBody({ data, loading }: { data: HoverCard; loading: boolean }) {
  return (
    <span className="block rounded-(--radius-card) border border-edge bg-surface p-3 text-left shadow-(--ep-shadow)">
      <span className="block font-serif text-sm text-ink">{data.title}</span>
      {data.description && (
        <span className="mt-0.5 block text-xs leading-relaxed text-ink-dim">{data.description}</span>
      )}
      {data.details.length > 0 && (
        <span className="mt-2 block">
          {data.details.map((detail, i) => (
            <span key={i} className="flex justify-between gap-4 text-xs leading-5">
              <span className="text-ink-faint">{detail.label}</span>
              <span className="truncate text-ink">{detail.value}</span>
            </span>
          ))}
        </span>
      )}
      {data.href && (
        <span className="mt-2 block">
          <CardLink href={data.href} className="text-xs text-accent-strong hover:underline" />
        </span>
      )}
      {loading && <span className="mt-1 block text-[11px] text-ink-faint">resolving…</span>}
    </span>
  );
}

export function EntityRefChip({ entref }: { entref: EntityRef }) {
  const open = usePanel((s) => s.open);
  const [active, setActive] = useState(false);
  const [opening, setOpening] = useState(false);
  const card = useQuery({
    queryKey: ["entity", entref.module, entref.kind, entref.ref_id],
    queryFn: () => api.resolveEntity(entref.module, entref.kind, entref.ref_id),
    enabled: active,
    staleTime: 60_000,
    retry: false,
  });
  const data = card.data ?? cardFromRef(entref);

  const isMailMessage = entref.module === "mail" && entref.kind === "message";

  const handleClick = () => {
    if (isMailMessage) {
      setOpening(true);
      api
        .readMailMessage(entref.module, entref.ref_id)
        .then((email) => open("email-reader", email, entref.title))
        .catch(() => open("entity-detail", data, entref.title))
        .finally(() => setOpening(false));
    } else {
      open("entity-detail", data, entref.title);
    }
  };

  return (
    <span
      className="group relative inline-block align-baseline"
      onMouseEnter={() => setActive(true)}
      onFocus={() => setActive(true)}
    >
      <button
        type="button"
        onClick={handleClick}
        disabled={opening}
        className={
          "inline-flex items-center gap-1 rounded-full border border-edge bg-surface-2 px-2 py-0.5 align-baseline text-[13px] leading-5 text-accent-strong transition-colors hover:border-accent" +
          (opening ? " opacity-60" : "")
        }
      >
        <AtSign size={11} className="shrink-0" />
        {entref.title}
      </button>
      <span className="invisible absolute top-full left-0 z-40 mt-1 block w-64 opacity-0 transition-opacity group-focus-within:visible group-focus-within:opacity-100 group-hover:visible group-hover:opacity-100">
        <HoverCardBody data={data} loading={card.isLoading} />
      </span>
    </span>
  );
}

/** A markdown `<a>` replacement: entity-scheme links become chips, others stay links. */
export function SmartLink({ href, children }: { href?: string; children?: ReactNode }) {
  const refs = useContext(EntityRefsContext);
  const match = href ? ENTITY_LINK_RE.exec(href) : null;
  if (match) {
    const [, module, kind, refId] = match;
    const known = refs.get(decodeURIComponent(refId));
    const entref: EntityRef = known ?? {
      ref_id: decodeURIComponent(refId),
      module: decodeURIComponent(module),
      kind: decodeURIComponent(kind),
      title: typeof children === "string" ? children : decodeURIComponent(refId),
      summary: null,
    };
    return <EntityRefChip entref={entref} />;
  }
  return (
    <a href={href} target="_blank" rel="noreferrer noopener" className="text-accent-strong underline">
      {children}
    </a>
  );
}
