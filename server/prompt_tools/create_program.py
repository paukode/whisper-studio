"""create_program — the browser-app builder flow as a native prompt tool."""

from server.executors import register_executor
from server.prompt_tools._render import render_prompt

TOOL = {
    "name": "create_program",
    "description": (
        "Builds a browser-based app, game, tool, dashboard, widget, or web page in "
        "HTML/CSS/JS. Use for new requests to create or make an app when the user "
        "has not already chosen a file layout; it first asks whether they want a "
        "single self-contained HTML page (delivered as an inline artifact card with "
        "preview and download) or a modular multi-file project saved to a folder. Do "
        "not use for slide decks or presentations, or for non-web programs such as "
        "Python scripts or CLI tools (build those with workspace tools or "
        "run_python). For a single chart of data use create_chart, and for a "
        "diagram that explains something use create_visual; both render inline "
        "with no build step. Returns an artifact card or created project files "
        "plus a short summary of what was built."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": (
                    "Full description of what to build, preserving all user-stated "
                    "requirements such as features, style, data, and any file or "
                    "folder preferences."
                ),
            },
        },
        "required": ["description"],
    },
}

BODY = r"""# Create Program

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

1. **Location.** If the user has not already named a target folder, ask with
   `ask_user_question` where the project should be saved (offer the connected
   workspace when one is present, plus "Other (please specify)"). Never write
   files without a confirmed destination.
2. **Plan the file layout.** Keep it minimal and conventional: `index.html`,
   `css/styles.css`, `js/main.js` (or `app.js`), plus extra modules only when
   the feature set genuinely needs them. Track the work with the task tools
   when the project has more than a couple of files.
3. **Gather context.** If the request mentions a person, company, product, or
   URL, use `web_search` or `web_fetch` first, as in Step 2A.
4. **Create each file with `ws_create_file`** (one call per file, so the user
   approves each write). Follow the same code quality rules as Step 2A, with
   CSS and JS in their own files instead of inline blocks; link them relatively
   from `index.html` so the folder opens and runs from disk.
5. **Close with a short summary**: the folder path, each created file with a
   one-line purpose, and how to open or serve the app."""


@register_executor("create_program", read_only=True, concurrent_safe=True, emits_prompt=True)
def exec_create_program(tool_input, transcript, current_attachments):
    return render_prompt(tool_input, BODY)
