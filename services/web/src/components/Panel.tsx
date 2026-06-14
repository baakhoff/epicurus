/**
 * The right-panel host (ADR-0018). A core-owned split-screen panel: a resizable
 * right column on wide screens, a bottom sheet on phones. It renders a **bounded,
 * core-defined** set of views (`entity-detail`, `email-reader`) from the data a
 * caller passes through the panel store — no module markup ever runs here.
 */
import { ChevronLeft, X } from "lucide-react";
import { Fragment, useCallback, useRef, useState, type PointerEvent } from "react";

import { CardLink } from "@/components/CardLink";
import { Sheet } from "@/components/ui";
import { EmailMessage, HoverCard } from "@/lib/contracts";
import { usePanel, usePanelCurrent, usePanelDepth, type PanelEntry } from "@/stores/panel";

/* ── views (core-defined vocabulary) ─────────────────────────────────────── */

function EntityDetailView({ payload }: { payload: unknown }) {
  const data = HoverCard.parse(payload);
  return (
    <div>
      <h3 className="font-serif text-lg text-ink">{data.title}</h3>
      {data.description && (
        <p className="mt-1 text-sm leading-relaxed text-ink-dim">{data.description}</p>
      )}
      {data.details.length > 0 && (
        <dl className="mt-4 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
          {data.details.map((detail, i) => (
            <Fragment key={i}>
              <dt className="text-ink-dim">{detail.label}</dt>
              <dd className="text-ink">{detail.value}</dd>
            </Fragment>
          ))}
        </dl>
      )}
      {data.href && (
        <CardLink
          href={data.href}
          className="mt-4 inline-flex items-center gap-1 text-sm text-accent-strong hover:underline"
        />
      )}
    </div>
  );
}

function EmailReaderView({ payload }: { payload: unknown }) {
  const mail = EmailMessage.parse(payload);
  return (
    <article>
      <h3 className="font-serif text-lg text-ink">{mail.subject}</h3>
      <div className="mt-1 text-xs text-ink-faint">
        {mail.from && <span>{mail.from}</span>}
        {mail.from && mail.date && <span> · </span>}
        {mail.date && <span>{mail.date}</span>}
      </div>
      <p className="mt-4 text-[15px] leading-relaxed whitespace-pre-wrap text-ink">{mail.body}</p>
    </article>
  );
}

function PanelBody({ entry }: { entry: PanelEntry }) {
  switch (entry.view) {
    case "entity-detail":
      return <EntityDetailView payload={entry.payload} />;
    case "email-reader":
      return <EmailReaderView payload={entry.payload} />;
    default:
      return null;
  }
}

/* ── hosts ───────────────────────────────────────────────────────────────── */

const MIN_WIDTH = 320;
const MAX_WIDTH = 640;

function DesktopPanel() {
  const current = usePanelCurrent();
  const depth = usePanelDepth();
  const back = usePanel((s) => s.back);
  const close = usePanel((s) => s.close);
  const [width, setWidth] = useState(384);
  const dragging = useRef(false);

  const onPointerDown = useCallback((e: PointerEvent<HTMLDivElement>) => {
    dragging.current = true;
    e.currentTarget.setPointerCapture(e.pointerId);
  }, []);
  const onPointerMove = useCallback((e: PointerEvent<HTMLDivElement>) => {
    if (!dragging.current) return;
    const next = window.innerWidth - e.clientX;
    setWidth(Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, next)));
  }, []);
  const onPointerUp = useCallback((e: PointerEvent<HTMLDivElement>) => {
    dragging.current = false;
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      /* pointer already released */
    }
  }, []);

  if (!current) return null;
  return (
    <aside className="hidden shrink-0 sm:flex" style={{ width }} aria-label="Detail panel">
      <div
        role="separator"
        aria-orientation="vertical"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        className="w-1 shrink-0 cursor-col-resize bg-edge transition-colors hover:bg-accent/40"
      />
      <div className="flex min-w-0 flex-1 flex-col border-l border-edge">
        <header className="flex items-center gap-2 border-b border-edge px-4 py-2.5">
          {depth > 1 && (
            <button
              onClick={back}
              aria-label="Back"
              className="rounded-md p-1 text-ink-dim hover:bg-surface-2 hover:text-ink"
            >
              <ChevronLeft size={16} />
            </button>
          )}
          <h2 className="min-w-0 flex-1 truncate font-serif text-base text-ink">
            {current.title || "Details"}
          </h2>
          <button
            onClick={close}
            aria-label="Close panel"
            className="rounded-md p-1 text-ink-dim hover:bg-surface-2 hover:text-ink"
          >
            <X size={16} />
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <PanelBody entry={current} />
        </div>
      </div>
    </aside>
  );
}

function MobilePanel() {
  const current = usePanelCurrent();
  const depth = usePanelDepth();
  const back = usePanel((s) => s.back);
  const close = usePanel((s) => s.close);
  return (
    <Sheet open={current !== null} onClose={close} title={current?.title || "Details"}>
      {current && (
        <>
          {depth > 1 && (
            <button
              onClick={back}
              className="mb-3 inline-flex items-center gap-1 text-sm text-ink-dim hover:text-ink"
            >
              <ChevronLeft size={15} /> back
            </button>
          )}
          <PanelBody entry={current} />
        </>
      )}
    </Sheet>
  );
}

/** Mounts both hosts; CSS shows the right one for the viewport. */
export function PanelHost() {
  return (
    <>
      <DesktopPanel />
      <div className="sm:hidden">
        <MobilePanel />
      </div>
    </>
  );
}
