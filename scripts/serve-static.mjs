// Tiny dependency-free static file server for the Playwright smoke test.
// Cross-platform (no python3, which is unreliable on Windows CI runners).
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";

const PORT = Number(process.env.PORT) || 4321;
const ROOT = process.cwd();
const TYPES = { ".js": "text/javascript", ".mjs": "text/javascript", ".html": "text/html", ".css": "text/css", ".json": "application/json" };

createServer(async (req, res) => {
  try {
    const urlPath = decodeURIComponent((req.url || "/").split("?")[0]);
    const rel = normalize(urlPath).replace(/^(\.\.[/\\])+/, "");
    const data = await readFile(join(ROOT, rel));
    res.writeHead(200, { "content-type": TYPES[extname(rel)] || "application/octet-stream" });
    res.end(data);
  } catch {
    res.writeHead(404);
    res.end("not found");
  }
}).listen(PORT, () => console.log(`serving ${ROOT} on http://localhost:${PORT}`));
