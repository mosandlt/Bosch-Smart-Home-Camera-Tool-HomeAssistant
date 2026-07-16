// Extract every <style>…</style> block embedded in the card's JS template
// strings into one plain CSS file, so static browser-compat tooling
// (stylelint + stylelint-browser-compat) can lint it. The card's CSS lives in
// JS template literals, so we must neutralise `${…}` interpolations — including
// NESTED ones (e.g. grid-template-columns built from a ternary of nested
// template literals) — by removing the innermost first, iteratively.
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";

const SRC_FILES = ["src/bosch-camera-card.js", "src/ai-alert-timeline-card.js"];
const OUT = "build/card-extracted.css";

const blocks = SRC_FILES.flatMap((srcPath) => {
  const src = readFileSync(srcPath, "utf8");
  return [...src.matchAll(/<style>([\s\S]*?)<\/style>/g)].map((m) => m[1]);
});
let css = blocks.join("\n\n");

// Neutralise interpolations innermost-first so nested ${ … ${x} … } collapses
// cleanly to a harmless placeholder value.
let prev;
do {
  prev = css;
  css = css.replace(/\$\{[^{}]*\}/g, "1px");
} while (css !== prev);
// Safety net: any leftover interpolation fragments + stray template backticks.
css = css.replace(/\$\{[\s\S]*?\}/g, "1px").replace(/`/g, "");

mkdirSync("build", { recursive: true });
writeFileSync(OUT, css);
console.log(`Extracted ${blocks.length} <style> blocks → ${OUT} (${css.length} chars)`);
