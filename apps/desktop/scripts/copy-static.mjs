// Copy the local pages' HTML and CSS next to their compiled scripts in dist/renderer.
import { copyFileSync, mkdirSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const from = join(root, "src", "renderer");
const to = join(root, "dist", "renderer");
mkdirSync(to, { recursive: true });
for (const name of readdirSync(from)) {
  if (name.endsWith(".html") || name.endsWith(".css")) copyFileSync(join(from, name), join(to, name));
}
