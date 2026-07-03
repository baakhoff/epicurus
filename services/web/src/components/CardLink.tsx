/**
 * A hover-card / entity-detail link (ADR-0019). A module resolver may return a
 * `HoverCardLink`; the core renders it here so the module never ships markup:
 *
 * - an **in-app** path (same-origin, starts with `/`) navigates via the router in the same
 *   tab — e.g. a cited knowledge doc opening in the Knowledge page (#143);
 * - an external `http(s)` link opens in a new tab;
 * - any other scheme (e.g. `javascript:`) is **dropped** — module-supplied data is never
 *   trusted to inject a script URL.
 */
import { Link } from "react-router-dom";

import type { HoverCardLink } from "@/lib/contracts";

/** A same-origin app route (e.g. `/m/knowledge/vault?doc=…`), not a protocol-relative `//`. */
function isInAppHref(url: string): boolean {
  return url.startsWith("/") && !url.startsWith("//");
}

/** The shared scheme guard for any module-supplied URL rendered as a link. */
export function isExternalHref(url: string): boolean {
  return /^https?:\/\//i.test(url);
}

export function CardLink({ href, className }: { href: HoverCardLink; className?: string }) {
  if (isInAppHref(href.url)) {
    return (
      <Link to={href.url} className={className}>
        {href.label} →
      </Link>
    );
  }
  if (isExternalHref(href.url)) {
    return (
      <a href={href.url} target="_blank" rel="noreferrer noopener" className={className}>
        {href.label} ↗
      </a>
    );
  }
  return null; // unsafe scheme — dropped
}
