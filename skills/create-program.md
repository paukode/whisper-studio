---
name: create_program
description: Builds a browser-based app, game, tool, dashboard, widget, or web page in HTML/CSS/JS. Use for new requests to create or make an app when the user has not already chosen a file layout; it first asks whether they want a single self-contained HTML page (delivered as an inline artifact card with preview and download) or a modular multi-file project saved to a folder. Do not use for slide decks or presentations (use presentation_builder), for requests that already specify a modular multi-file project (use frontend_design directly), or for non-web programs such as Python scripts or CLI tools (build those with workspace tools or run_python). Returns an artifact card or created project files plus a short summary of what was built.
triggers: create app, build app, make a game, web app, dashboard, widget, web page, website, calculator, single html page
input_schema:
  description:
    type: string
    required: true
    description: Full description of what to build, preserving all user-stated requirements such as features, style, data, and any file or folder preferences.
---

# Create Program

You help users create browser-based programs and apps.

If the request is for a non-web program (a Python script, CLI tool, backend service),
say so and build it with workspace tools or run_python instead of forcing the HTML flow.

## Step 1: Determine the project type

Skip the question ONLY when the user explicitly named a format ("single page",
"one html file", "artifact", "multi-file", "modular") or gave a target folder. A
plain request like "make me a timer app" does not imply a format: ask. The user
chose this ask-first design so they keep control over where code ends up.

Call `ask_user_question` with:
- question: "What type of project would you like?"
- options:
  - "Single HTML page (preview and download inline)"
  - "Modular app (separate HTML, CSS, JS files saved to a folder)"
  - "Other (please specify)"

Wait for the user's response before continuing.

## Step 2A: Single HTML page

1. If the request mentions a person, company, product, or URL, use `web_search` or
   `web_fetch` to gather context first.
2. Generate a complete, self-contained HTML document with all CSS in `<style>` and all
   JS in `<script>`.
3. Call `create_artifact` with:
   - **title**: a short descriptive title (e.g. "Todo App", "Weather Dashboard") (required)
   - **html**: the complete HTML source, starting with `<!DOCTYPE html>`, fully standalone (required)
   - **description**: a 1-2 sentence summary of what the program does
4. After the artifact is created, write a brief explanation of what you built and how
   to use it.

**Never output the HTML as text in your response. The full document goes through
`create_artifact` only**; pasted code blocks flood the chat and skip the preview card.

### Code quality rules for single HTML

- Complete `<!DOCTYPE html>` document with `<html>`, `<head>`, `<body>`
- All CSS in a single `<style>` block, with CSS variables for the color palette
- All JS in a single `<script>` block at the end of `<body>`, no inline event handlers
- Google Fonts via CDN `<link>`: choose distinctive fonts, not Arial or system defaults
- Responsive layout that works on mobile and desktop
- Dark background with sharp accent colors preferred (unless the user requests otherwise)
- `const` and `let` only, no `var`; no inline `style=""` attributes
- No external JS/CSS dependencies except CDN fonts

## Step 2B: Modular app

The modular flow is owned by the `frontend_design` skill; do not re-implement it here.
Call `skill_invoke` with:
- skill_name: `frontend_design`
- input: the user's full request, plus the chosen folder if one was already given

Then follow the instructions it returns. This keeps one canonical modular flow
(location question, task tracking, per-file approval via `ws_create_file`, closing
summary) instead of two drifting copies.
