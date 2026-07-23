# Whisper Studio - documentation site

A hand-built, self-contained documentation website for Whisper Studio. Plain
HTML + CSS + vanilla JS: **no build step, no dependencies, and zero external
network calls** (fonts are self-hosted), so it renders correctly on an
isolated / internal-only network.

## Preview locally

Serve the folder over HTTP (module-free classic scripts also work from
`file://`, but HTTP is closest to production):

```sh
cd docs
python3 -m http.server 8123
# open http://127.0.0.1:8123
```

## Deploy

The site is prebuilt static files, so publishing is just copying `docs/`
as-is - no build step. All links are relative, so it works at any base path (a
project-pages subpath, a custom domain, or a local file).

### GitHub Pages

`.github/workflows/pages.yml` uploads `docs/` and deploys it with the
official Pages actions (no build step).

- One-time: in the repo, **Settings > Pages > Source = "GitHub Actions"**.
- Push to `main` (or run the workflow manually via **Actions > Deploy docs to
  GitHub Pages > Run workflow**).
- Served at `https://<owner>.github.io/<repo>/`.

No Pages at all? The same files work from any static host (nginx, `python3 -m
http.server`) or opened directly.

## Structure

```
docs/
  index.html            Overview / landing (hero + quick guideline)
  requirements.html …   Get started
  tut-*.html            Tutorials
  arch-*.html           Architecture (overview + chat pipeline + subsystems)
  ref-*.html            Reference
  assets/
    theme.css           Design tokens (ported from the app) + fonts + reset
    site.css            Header, sidebar, TOC, footer, cards, buttons
    prose.css           Content typography, code, tables, callouts, steps
    diagram.css         Interactive diagram styling
    site.js             Builds the chrome from nav.js; theme toggle; search
    nav.js              Single source-of-truth navigation model
    diagram.js          Interactive click-to-glow diagram engine
    diagrams/*.js       Per-page diagram configs (grid layout)
    fonts/*.woff2       Self-hosted Outfit + IBM Plex Mono
```

## Authoring

- **Add a page:** create `docs/<name>.html`, copy the `<head>` and script
  tags from an existing page, write your content inside
  `<main class="doc" data-page="...">`, and add an entry to `assets/nav.js`. The
  header, sidebar, breadcrumb, on-this-page TOC, and prev/next are injected by
  `site.js`; you only write the `<main>` content.
- **Add a diagram:** create `assets/diagrams/<name>.js` calling
  `WSDiagram.mount("<name>-diagram", { ... })` with grid `col`/`row`
  coordinates (see `assets/diagrams/chat-pipeline.js`), drop a
  `<div id="<name>-diagram" class="diagram">` into the page, and include the
  config script after `site.js`.
- **Theme:** all colors come from CSS variables in `theme.css` (light + dark).
  Use the existing variables; never hard-code a color.
