import { build, context } from "esbuild";
import { fileURLToPath } from "url";
import { dirname, resolve } from "path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const outdir = resolve(__dirname, "..", "static", "js");
const watch = process.argv.includes("--watch");

const opts = {
  entryPoints: [resolve(__dirname, "src/main.ts")],
  outfile: resolve(outdir, "planner.js"),
  bundle: true,
  format: "iife",
  globalName: "MissionPlanner",
  // Watch/dev: sourcemaps; production build: minify, no .map.
  sourcemap: watch,
  minify: !watch,
  target: ["es2020"],
  loader: { ".css": "text" },
  logLevel: "info",
};

if (watch) {
  const ctx = await context(opts);
  await ctx.watch();
} else {
  await build(opts);
}
