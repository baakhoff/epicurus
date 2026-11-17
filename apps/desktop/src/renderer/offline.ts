/** The page shown instead of a blank window when the server cannot be reached. */
(() => {
  const params = new URLSearchParams(location.search);
  const kind = params.get("kind") ?? "unreachable";
  const titles: Record<string, string> = {
    unreachable: "Server not reachable",
    timeout: "Server not answering",
    certificate: "Certificate not trusted",
    crashed: "The page stopped working",
  };
  (document.getElementById("title") as HTMLElement).textContent =
    titles[kind] ?? "Server not reachable";
  document.title = `${titles[kind] ?? "Server not reachable"} — Epicurus`;
  (document.getElementById("message") as HTMLElement).textContent =
    params.get("message") ?? "The server could not be reached.";
  (document.getElementById("server") as HTMLElement).textContent = params.get("server") ?? "";
  const retry = document.getElementById("retry") as HTMLButtonElement;
  retry.addEventListener("click", () => {
    retry.disabled = true;
    void window.epicurusShell.retry();
  });
  (document.getElementById("change") as HTMLButtonElement).addEventListener(
    "click",
    () => void window.epicurusShell.changeServer(),
  );
  retry.focus();
})();
