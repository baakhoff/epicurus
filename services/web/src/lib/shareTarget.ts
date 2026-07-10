/**
 * The Cache Storage keys the share-target service worker handler (`src/sw.ts`) and the chat
 * screen agree on (#493) — kept in one place so the two independently-bundled sides (the SW is
 * its own Rollup entry, injectManifest strategy) can't drift out of sync on a typo'd string.
 */
export const SHARE_CACHE = "share-target-v1";
export const SHARE_META_KEY = "/share-payload/meta";
export const SHARE_FILE_KEY = "/share-payload/file";
/** Carries the shared file's original name across the Cache API, which stores bytes only. */
export const SHARE_FILE_NAME_HEADER = "X-Share-File-Name";

/** The share-target payload's text fields, as the SW's fetch handler stashes them. */
export interface ShareMeta {
  title: string;
  text: string;
  url: string;
  hasFile: boolean;
}
