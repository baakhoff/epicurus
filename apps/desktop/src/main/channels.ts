/** IPC channel names. `src/preload/preload.ts` repeats them (a sandboxed preload cannot import
 *  a local module); `test/unit/preload.test.ts` keeps the two lists equal. */
export const CHANNELS = {
  checkServer: "epicurus-shell:check-server",
  connect: "epicurus-shell:connect",
  cancelConnect: "epicurus-shell:cancel-connect",
  retry: "epicurus-shell:retry",
  changeServer: "epicurus-shell:change-server",
  backToServer: "epicurus-shell:back-to-server",
} as const;
