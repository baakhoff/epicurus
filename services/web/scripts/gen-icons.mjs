/**
 * Rasterize the ε mark into the PWA icon set (one-off; outputs are committed).
 * Run: npm run icons
 */
import { Resvg } from "@resvg/resvg-js";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

/** The ε mark on its canvas. pad > 0 adds the maskable safe zone (full bleed). */
function mark(pad) {
  const offset = -pad;
  const span = 64 + 2 * pad;
  const corner = pad > 0 ? 0 : 14;
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${offset} ${offset} ${span} ${span}">
  <rect x="${offset}" y="${offset}" width="${span}" height="${span}" rx="${corner}" fill="#121411"/>
  <path d="M 46 20 A 17.5 17.5 0 1 0 46 44" stroke="#C9A961" stroke-width="6" stroke-linecap="round" fill="none"/>
  <line x1="29" y1="32" x2="45" y2="32" stroke="#C9A961" stroke-width="6" stroke-linecap="round"/>
</svg>`;
}

const out = join(root, "public", "icons");
mkdirSync(out, { recursive: true });

const renders = [
  ["icon-192.png", mark(0), 192],
  ["icon-512.png", mark(0), 512],
  ["icon-maskable-512.png", mark(13), 512],
  ["apple-touch-icon.png", mark(0), 180],
];

for (const [name, svg, size] of renders) {
  const png = new Resvg(svg, { fitTo: { mode: "width", value: size } }).render().asPng();
  writeFileSync(join(out, name), png);
  console.log(`${name} (${size}px, ${png.length} bytes)`);
}
