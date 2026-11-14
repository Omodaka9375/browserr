import { Resvg } from "@resvg/resvg-js";
import { readFileSync, writeFileSync } from "node:fs";

// 1200x630 OG banner: dark tile from icon.svg, chevron mark, wordmark.
// Rendered with system fonts (resvg fontdb). Committed output: docs/og.png.
const icon = readFileSync("../docs/icon.svg", "utf8")
  // Inline only the chevron paths from the source icon.
  .match(/<g filter="url\(#glow\)"[\s\S]*?<\/g>/)[0];

const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 630" width="1200" height="630">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#171717"/>
      <stop offset="1" stop-color="#0c0c0c"/>
    </linearGradient>
    <filter id="glow" x="-40%" y="-40%" width="180%" height="180%">
      <feGaussianBlur stdDeviation="6" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
  </defs>
  <rect width="1200" height="630" fill="url(#bg)"/>
  <rect x="1" y="1" width="1198" height="628" fill="none" stroke="#333" stroke-width="2"/>

  <g transform="translate(80,79) scale(0.73)">
    ${icon}
  </g>

  <text x="500" y="290" font-family="Segoe UI, Arial, sans-serif" font-size="96"
        font-weight="700" fill="#e8e8e6">Browserr</text>
  <text x="504" y="360" font-family="Segoe UI, Arial, sans-serif" font-size="36"
        fill="#9a9a9a">browser engine for AI agents</text>
  <text x="504" y="430" font-family="Segoe UI, Arial, sans-serif" font-size="30"
        fill="#6a6a6a">browserr.dev</text>
</svg>`;

const png = new Resvg(svg, { fitTo: { mode: "width", value: 1200 } }).render().asPng();
writeFileSync("../docs/og.png", png);
console.log("docs/og.png", png.length, "bytes");
