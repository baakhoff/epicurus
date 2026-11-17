/** What the preload exposes to the app's own local pages (never to a web page). */
type CheckOutcome =
  | {
      ok: true;
      url: string;
      host: string;
      version: string | null;
      signInRequired: boolean;
      needsInsecureConfirmation: boolean;
    }
  | { ok: false; kind: string; message: string };

type ConnectOutcome = { ok: true } | { ok: false; message: string };

interface EpicurusShell {
  checkServer(address: string): Promise<CheckOutcome>;
  connect(url: string, confirmedInsecure: boolean): Promise<ConnectOutcome>;
  cancelConnect(): Promise<void>;
  retry(): Promise<void>;
  changeServer(): Promise<void>;
  backToServer(): Promise<void>;
}

interface Window {
  epicurusShell: EpicurusShell;
}
