/** The host bar shown above a page that is not on the server (an identity provider, an
 *  account-connect consent screen): which host this is, and a way back. */
(() => {
  const params = new URLSearchParams(location.search);
  const host = params.get("host") ?? "";
  const insecure = params.get("insecure") === "1";
  (document.getElementById("host") as HTMLElement).textContent = host;
  const lock = document.getElementById("lock") as HTMLElement;
  if (insecure) {
    lock.classList.add("insecure");
    (document.getElementById("scheme-note") as HTMLElement).textContent = " (not encrypted)";
  }
  lock.title = insecure ? "Not encrypted (http)" : "Encrypted (https)";
  (document.getElementById("back") as HTMLButtonElement).addEventListener(
    "click",
    () => void window.epicurusShell.backToServer(),
  );
})();
