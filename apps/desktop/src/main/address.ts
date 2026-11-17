/**
 * Server addresses — what the user types on the "Connect to a server" page, turned into the one
 * origin the window loads.
 *
 * The web UI calls the API with root-relative `/platform/...` paths, so it only works when it is
 * loaded from the root of the server's own origin. Whatever the user pastes (a bare host, a deep
 * link into the app, a trailing slash) is therefore reduced to `scheme://host[:port]/`.
 */

export type NormalisedAddress = {
  ok: true;
  /** The address the window loads: the origin plus a trailing `/`. */
  url: string;
  /** `scheme://host[:port]` — what navigation and permission decisions compare against. */
  origin: string;
  /** The host (and port, if not the default) — what the UI shows. */
  host: string;
  /** Plain `http:` — traffic, including the session cookie, is not encrypted. */
  insecure: boolean;
  /** A loopback host (`localhost`, `*.localhost`, `127.0.0.0/8`, `::1`). */
  loopback: boolean;
};

export type AddressError = { ok: false; error: string };

const SCHEME_RE = /^[a-z][a-z0-9+.-]*:\/\//i;

/** Whether `hostname` (as `URL.hostname` gives it) is a loopback name or address. */
export function isLoopbackHost(hostname: string): boolean {
  const host = hostname.toLowerCase().replace(/\.$/, "");
  if (host === "localhost" || host.endsWith(".localhost")) return true;
  if (host === "[::1]" || host === "::1") return true;
  const parts = host.split(".");
  if (parts.length === 4 && parts.every((p) => /^\d{1,3}$/.test(p) && Number(p) <= 255)) {
    return parts[0] === "127";
  }
  return false;
}

/**
 * Turn user input into a server origin, or say plainly why it is not one.
 *
 * - Surrounding whitespace is ignored.
 * - Without a scheme, `https://` is assumed — except for a loopback host, where `http://` is
 *   (a server on this machine is almost never behind TLS).
 * - Only `http:` and `https:` are accepted; credentials in the address are refused (they would
 *   be sent on every request and saved in the settings file).
 * - Path, query and fragment are dropped: the web UI lives at the root of its origin.
 */
export function normaliseServerAddress(input: string): NormalisedAddress | AddressError {
  const trimmed = input.trim();
  if (trimmed === "") return { ok: false, error: "Enter the address of your epicurus server." };
  if (/\s/.test(trimmed)) return { ok: false, error: "An address cannot contain spaces." };

  let candidate = trimmed;
  if (!SCHEME_RE.test(candidate)) {
    // `mailto:x`, `file:foo`, `javascript:…` — a scheme without `//`. `host:8080` is not one:
    // what follows its colon is a port.
    if (/^[a-z][a-z0-9+.-]*:(?!\d+(?:[/?#]|$))/i.test(candidate)) {
      return { ok: false, error: "Only http:// and https:// addresses are supported." };
    }
    const hostPart = candidate.split(/[/?#]/, 1)[0] ?? "";
    let probe: URL | null;
    try {
      probe = new URL(`http://${hostPart}`);
    } catch {
      probe = null;
    }
    candidate = `${probe && isLoopbackHost(probe.hostname) ? "http" : "https"}://${candidate}`;
  }

  let url: URL;
  try {
    url = new URL(candidate);
  } catch {
    return { ok: false, error: "That does not look like a web address." };
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    return { ok: false, error: "Only http:// and https:// addresses are supported." };
  }
  if (url.username !== "" || url.password !== "") {
    return { ok: false, error: "Leave the user name and password out of the address." };
  }
  if (url.hostname === "") return { ok: false, error: "The address has no host name." };

  const origin = url.origin;
  return {
    ok: true,
    url: `${origin}/`,
    origin,
    host: url.host,
    insecure: url.protocol === "http:",
    loopback: isLoopbackHost(url.hostname),
  };
}

/** Plain HTTP to a host that is not this machine — the case the user must confirm. */
export function needsInsecureConfirmation(address: NormalisedAddress): boolean {
  return address.insecure && !address.loopback;
}
