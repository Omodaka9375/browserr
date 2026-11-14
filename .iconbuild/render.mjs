import { Resvg } from "@resvg/resvg-js";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";

const svg = readFileSync("../docs/icon.svg", "utf8");
mkdirSync("../engine/assets", { recursive: true });

const sizes = [16, 24, 32, 48, 64, 128, 256];
for (const s of sizes) {
  const r = new Resvg(svg, { fitTo: { mode: "width", value: s } });
  const png = r.render().asPng();
  writeFileSync(`icon-${s}.png`, png);
  if (s === 64) writeFileSync("../engine/assets/icon.png", png);
  console.log(`icon-${s}.png ${png.length} bytes`);
}
console.log("done");
