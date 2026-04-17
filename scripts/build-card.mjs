#!/usr/bin/env node
// Build script: strips JS-level comments from src/bosch-camera-card.js via
// terser (AST-aware, CSS comments inside template literals are preserved)
// and writes the stripped version to www/bosch-camera-card.js with a
// minimal header banner. Saves ~30% on the gzipped wire payload served
// to every browser that loads a Bosch camera dashboard.
//
// Usage: node scripts/build-card.mjs
// Requires: npm install terser (or run via npx)

import { minify } from "terser";
import { readFileSync, writeFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, resolve } from "path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(__dirname, "..");

const BANNER = `/**
 * Bosch Camera Card — Custom Lovelace Card
 * Repo:    https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant
 * Docs:    https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/blob/main/docs/card-architecture.md
 * License: MIT
 *
 * This file is auto-generated from src/bosch-camera-card.js by
 * scripts/build-card.mjs. Do not edit directly — edit the src file and
 * rebuild. Comments are stripped to reduce the gzipped payload size.
 */
`;

const AUTOPLAY_BANNER = `/**
 * Bosch Camera Autoplay Fix — Lovelace helper script
 * Repo:    https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant
 * License: MIT
 */
`;

async function stripFile(srcPath, outPath, banner) {
  const src = readFileSync(srcPath, "utf8");
  const result = await minify(src, {
    compress: false,
    mangle: false,
    format: {
      comments: false,
      beautify: true,
      indent_level: 2,
      semicolons: true,
    },
  });
  if (result.error) throw result.error;
  writeFileSync(outPath, banner + result.code);
  return { srcBytes: src.length, outBytes: result.code.length + banner.length };
}

const card = await stripFile(
  resolve(repoRoot, "src/bosch-camera-card.js"),
  resolve(repoRoot, "www/bosch-camera-card.js"),
  BANNER,
);
console.log(`bosch-camera-card.js:          ${card.srcBytes} -> ${card.outBytes} bytes`);

// Autoplay fix: strip in place (no src/ version — it's small and doesn't
// need the same build workflow, but we still strip comments for consistency)
const autoplay = await stripFile(
  resolve(repoRoot, "www/bosch-camera-autoplay-fix.js"),
  resolve(repoRoot, "www/bosch-camera-autoplay-fix.js"),
  AUTOPLAY_BANNER,
);
console.log(`bosch-camera-autoplay-fix.js:  already stripped`);

console.log("Done.");
