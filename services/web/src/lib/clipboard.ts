/**
 * Copy text to the clipboard, reporting success. Prefers the async clipboard API and
 * falls back to the legacy selection path when it is unavailable or refuses — the API
 * only exists on secure origins, and a self-hosted PWA reached over plain-HTTP LAN
 * (#460) has no `navigator.clipboard` at all.
 */
export async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      /* blocked permission or unfocused document — try the legacy path */
    }
  }
  const scratch = document.createElement("textarea");
  scratch.value = text;
  scratch.setAttribute("readonly", "");
  scratch.style.position = "fixed";
  scratch.style.opacity = "0";
  document.body.appendChild(scratch);
  scratch.select();
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch {
    copied = false;
  }
  scratch.remove();
  return copied;
}
