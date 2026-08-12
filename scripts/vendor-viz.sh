#!/usr/bin/env bash
# Copy the Vega runtime out of node_modules into static/viz/vendor/.
#
# The chart host page loads these as plain <script src> from the backend's
# /static mount, so charts render with no network access and no bundler.
# The copies are COMMITTED: a fresh clone (or the packaged .app, which rsyncs
# static/ but not node_modules/) must render charts without an npm install.
#
# Upgrade path: npm update vega vega-lite && npm run vendor:viz && commit.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/static/viz/vendor"
mkdir -p "$OUT"

copy() {
    local src="$ROOT/node_modules/$1" dest="$OUT/$2"
    [[ -f "$src" ]] || { echo "missing $src (run npm install)" >&2; exit 1; }
    cp "$src" "$dest"
    echo "  $2  $(du -h "$dest" | cut -f1)"
}

echo "vendoring vega runtime -> static/viz/vendor/"
copy "vega/build/vega.min.js" "vega.min.js"
copy "vega-lite/build/vega-lite.min.js" "vega-lite.min.js"

node -e '
const fs = require("fs");
const v = (p) => require(`${process.argv[1]}/node_modules/${p}/package.json`).version;
fs.writeFileSync(
  `${process.argv[1]}/static/viz/vendor/VERSIONS.txt`,
  `vega ${v("vega")}\nvega-lite ${v("vega-lite")}\n`,
);
' "$ROOT"
echo "  VERSIONS.txt"
