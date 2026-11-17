/** The "Connect to a server" page: check the address in the main process, confirm plain HTTP
 *  to another machine, then connect. */
(() => {
  const params = new URLSearchParams(location.search);
  const form = document.getElementById("connect-form") as HTMLFormElement;
  const input = document.getElementById("address") as HTMLInputElement;
  const status = document.getElementById("status") as HTMLDivElement;
  const submit = document.getElementById("submit") as HTMLButtonElement;
  const cancel = document.getElementById("cancel") as HTMLButtonElement;
  const actions = document.getElementById("actions") as HTMLDivElement;
  const warning = document.getElementById("insecure-warning") as HTMLDivElement;
  const warningText = document.getElementById("insecure-text") as HTMLSpanElement;
  const confirmInsecure = document.getElementById("insecure-confirm") as HTMLButtonElement;
  const cancelInsecure = document.getElementById("insecure-cancel") as HTMLButtonElement;

  input.value = params.get("address") ?? "";
  if (params.get("cancel") === "1") cancel.hidden = false;

  let pendingUrl: string | null = null;

  function show(text: string, tone: "" | "error" | "ok" = ""): void {
    status.textContent = text;
    status.className = tone ? `status ${tone}` : "status";
  }

  function busy(on: boolean): void {
    submit.disabled = on;
    input.disabled = on;
  }

  async function connect(url: string, confirmedInsecure: boolean): Promise<void> {
    busy(true);
    const result = await window.epicurusShell.connect(url, confirmedInsecure);
    if (!result.ok) {
      busy(false);
      show(result.message, "error");
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    warning.hidden = true;
    actions.hidden = false;
    pendingUrl = null;
    busy(true);
    show("Checking…");
    void window.epicurusShell.checkServer(input.value).then(async (result) => {
      if (!result.ok) {
        busy(false);
        show(result.message, "error");
        input.focus();
        return;
      }
      const what = result.version ? `epicurus ${result.version}` : "an epicurus server";
      const auth = result.signInRequired ? " — you will be asked to sign in" : "";
      show(`Found ${what} at ${result.host}${auth}.`, "ok");
      if (result.needsInsecureConfirmation) {
        busy(false);
        pendingUrl = result.url;
        warningText.textContent =
          ` ${result.host} is reached over plain http://. Anyone on the network between you ` +
          "and the server can read and change what you send, including your sign-in. Use " +
          "https:// unless this network is one you trust.";
        warning.hidden = false;
        actions.hidden = true;
        cancelInsecure.focus();
        return;
      }
      await connect(result.url, false);
    });
  });

  confirmInsecure.addEventListener("click", () => {
    if (pendingUrl) void connect(pendingUrl, true);
  });
  cancelInsecure.addEventListener("click", () => {
    warning.hidden = true;
    actions.hidden = false;
    pendingUrl = null;
    show("");
    input.focus();
  });
  cancel.addEventListener("click", () => void window.epicurusShell.cancelConnect());
})();
