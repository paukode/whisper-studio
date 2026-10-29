"""When a reply should carry a picture: the model picks the form of an answer
itself, including a diagram or chart the user did not ask for.

Registered as the ``visual_choice`` section of the cloud chat prompt
(server/prompts/__init__.py). Fixed text with no builder on the cached
layer, and brief mode is named in the text instead of switching the section
off, so a mode flip never rewrites the cached prefix. Headless runs,
scheduled tasks, agents and the on-device prompt build their own system
prompts and never read this one.

The theme half needs no per-request text: diagram and chart cards style
themselves from the app's live tokens (src/components/chat/VizCard.tsx), and
the HTML page card adds the same tokens as --ws-* variables
(src/utils/artifactTheme.ts), so the model only has to stay out of the way.
"""

VISUAL_CHOICE = (
    "\n\n## Choosing the form of a reply\n"
    "Before you answer, decide which form serves the reader best, also when the user did "
    "not ask for a visual. A process, flow, architecture, hierarchy, state machine, timeline, "
    "or a comparison of three or more options on several points: draw it with create_visual "
    "next to the text. Real numbers from the conversation, a file or a tool result that show "
    "a trend, a ranking, a distribution or a share: chart them with create_chart; never "
    "invent data to have something to chart. A few exact values side by side: a markdown "
    "table. A short, factual or conversational answer: text only. Add at most one visual "
    "the user did not ask for per reply, keep the text answer complete without it, and add "
    "none when the user asks for text only, in brief mode, or when the picture would only "
    "repeat one sentence. Diagrams, charts and HTML pages take the user's active Whisper "
    "theme and light or dark mode on their own: choose no colors or fonts for them unless "
    "the user asks for a different look."
)
