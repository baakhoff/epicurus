/**
 * MailHtmlBody — render an email's HTML body safely (ADR-0097, #627).
 *
 * The module supplies the raw HTML (data); the shell renders it. Two independent safety layers:
 *
 * 1. **Inert sanitize.** The HTML is parsed with `DOMParser` (no execution, no resource loads),
 *    then `<script>`/`<link>`/`<iframe>`/`<form>`/… are removed, every `on*=` handler and
 *    `javascript:`/`vbscript:` URL is stripped, `cid:` images are rewritten to the module's
 *    same-origin attachment proxy, and remote images are removed by default (revealed on an
 *    explicit "Load images"). The raw HTML never touches the live DOM.
 * 2. **Sandboxed iframe.** The sanitized result is rendered via `srcDoc` in an iframe whose
 *    sandbox grants `allow-same-origin` (so the parent can auto-size it and `cid:` proxy images
 *    carry the session cookie) and `allow-popups` (so links open in a new tab) — but **never**
 *    `allow-scripts`, so no email JS can ever run, and never `allow-forms`. A CSP meta backs
 *    this up. The email's CSS is confined to the frame — no bleed into the app shell.
 *
 * Remote images are blocked by default because a remote `<img>` is the classic tracking pixel;
 * loading them is a deliberate, per-message choice.
 */
import { ImageOff } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui";
import type { MailAttachment } from "@/lib/contracts";

/** The sanitized inner HTML + how many remote images were blocked (drives the "Load images" bar). */
type Prepared = { inner: string; blockedRemote: number };

/** Elements removed wholesale — script/style-injection and external-resource vectors. */
const STRIP_ELEMENTS = "script, link, meta, base, iframe, object, embed, form, noscript, template";

function isUnsafeUrl(value: string): boolean {
  return /^\s*(?:javascript|vbscript|data:text\/html)/i.test(value);
}

/**
 * Inertly sanitize *html* and resolve/gate its images. Pure DOM work on a detached document —
 * nothing here executes scripts or loads resources.
 */
function prepare(
  html: string,
  attachments: MailAttachment[],
  messageId: string,
  attachmentUrl: ((messageId: string, attachmentId: string) => string) | undefined,
  loadRemote: boolean,
): Prepared {
  const doc = new DOMParser().parseFromString(html, "text/html");
  doc.querySelectorAll(STRIP_ELEMENTS).forEach((el) => el.remove());

  // Strip event handlers + javascript: URLs from every element.
  doc.querySelectorAll("*").forEach((el) => {
    for (const attr of [...el.attributes]) {
      const name = attr.name.toLowerCase();
      if (name.startsWith("on")) el.removeAttribute(attr.name);
      else if (
        (name === "href" || name === "src" || name === "xlink:href") &&
        isUnsafeUrl(attr.value)
      )
        el.removeAttribute(attr.name);
    }
  });

  // Inline images: cid:<id> → the module's attachment proxy (never a direct provider URL).
  const byCid = new Map<string, MailAttachment>();
  for (const att of attachments) if (att.content_id) byCid.set(att.content_id, att);

  let blockedRemote = 0;
  doc.querySelectorAll("img").forEach((img) => {
    const src = img.getAttribute("src") ?? "";
    const cid = /^cid:(.+)$/i.exec(src);
    if (cid) {
      const id = decodeURIComponent(cid[1]).replace(/^<|>$/g, "");
      const att = byCid.get(id);
      if (att && attachmentUrl) img.setAttribute("src", attachmentUrl(messageId, att.id));
      else img.removeAttribute("src"); // unresolved inline image → don't leave a broken cid:
      return;
    }
    if (/^https?:/i.test(src)) {
      if (loadRemote) return; // user opted in — keep the remote src
      img.setAttribute("data-remote-src", src); // stash so "Load images" can restore it
      img.removeAttribute("src");
      img.removeAttribute("srcset");
      blockedRemote += 1;
    }
  });

  // Links open in a new tab, severed from this document.
  doc.querySelectorAll("a[href]").forEach((a) => {
    a.setAttribute("target", "_blank");
    a.setAttribute("rel", "noopener noreferrer nofollow");
  });

  return { inner: doc.body.innerHTML, blockedRemote };
}

/** The document wrapper: a CSP, base styles (emails assume a white canvas), and the sanitized body. */
function buildSrcDoc(inner: string, loadRemote: boolean): string {
  // With `allow-same-origin` the frame shares the app origin, so `'self'` matches the same-origin
  // cid proxy; remote hosts are only allowed once the user loads images. Scripts are always off.
  const imgSrc = loadRemote ? "'self' data: https: http:" : "'self' data:";
  const csp = [
    "default-src 'none'",
    `img-src ${imgSrc}`,
    "style-src 'unsafe-inline'",
    "font-src data:",
    "media-src 'none'",
    "frame-src 'none'",
    "connect-src 'none'",
  ].join("; ");
  // Emails hardcode dark text on an assumed white background, so render on white in both app
  // themes (a mainstream mail-client convention) — the cheap dark-mode legibility pass (#627).
  const style =
    "html{background:#fff;color:#111;margin:0}" +
    "body{margin:0;padding:12px;font:14px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI'," +
    "Roboto,Helvetica,Arial,sans-serif;color:#111;word-break:break-word;overflow-wrap:anywhere}" +
    "img{max-width:100%;height:auto}a{color:#0b57d0}" +
    "table{max-width:100%}blockquote{margin:0 0 0 12px;padding-left:12px;border-left:2px solid #ddd}";
  return (
    "<!doctype html><html><head><meta charset=\"utf-8\">" +
    `<meta http-equiv="Content-Security-Policy" content="${csp}">` +
    `<base target="_blank"><style>${style}</style></head><body>${inner}</body></html>`
  );
}

export function MailHtmlBody({
  html,
  messageId,
  attachments,
  attachmentUrl,
}: {
  html: string;
  messageId: string;
  attachments: MailAttachment[];
  /** Builds a same-origin proxy URL for one attachment; required for inline `cid:` images to
   *  load. When omitted, inline images simply don't render (no broken `cid:` requests). */
  attachmentUrl?: (messageId: string, attachmentId: string) => string;
}) {
  const [loadRemote, setLoadRemote] = useState(false);
  const [height, setHeight] = useState(120);
  const frameRef = useRef<HTMLIFrameElement>(null);

  const { inner, blockedRemote } = useMemo(
    () => prepare(html, attachments, messageId, attachmentUrl, loadRemote),
    [html, attachments, messageId, attachmentUrl, loadRemote],
  );
  const srcDoc = useMemo(() => buildSrcDoc(inner, loadRemote), [inner, loadRemote]);

  // Auto-size to content: with `allow-same-origin` the parent can read the frame's document
  // (no email script involved — scripts are off). Re-measure as (proxied/loaded) images arrive.
  const measure = useCallback(() => {
    const doc = frameRef.current?.contentDocument;
    if (!doc?.body) return;
    setHeight(Math.max(doc.documentElement?.scrollHeight ?? 0, doc.body.scrollHeight, 40));
  }, []);

  useEffect(() => {
    const doc = frameRef.current?.contentDocument;
    if (!doc?.body || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(doc.body);
    return () => observer.disconnect();
  }, [measure, srcDoc]);

  return (
    <div className="mt-4">
      {blockedRemote > 0 && !loadRemote && (
        <div className="mb-2 flex flex-wrap items-center gap-2 rounded-(--radius-field) border border-edge bg-surface-2 px-3 py-2 text-xs text-ink-dim">
          <ImageOff size={14} className="shrink-0 text-ink-faint" />
          <span className="min-w-0 flex-1">
            Remote images are blocked to protect your privacy (they can track when a message is
            opened).
          </span>
          <Button variant="outline" size="sm" onClick={() => setLoadRemote(true)}>
            Load images
          </Button>
        </div>
      )}
      <iframe
        ref={frameRef}
        title="Email message"
        // No allow-scripts (email JS can never run) and no allow-forms; allow-same-origin lets
        // the parent auto-size it and cid proxy images carry the session cookie; allow-popups so
        // links open in a new tab (ADR-0097, #627).
        sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"
        srcDoc={srcDoc}
        onLoad={measure}
        className="w-full border-0 bg-white"
        style={{ height, colorScheme: "light" }}
      />
    </div>
  );
}
