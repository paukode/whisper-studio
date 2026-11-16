"""When a reply should carry a picture: the model picks the form of an answer
itself, including a diagram or chart the user did not ask for.

Registered as the ``visual_choice`` section of the cloud chat prompt
(server/prompts/__init__.py). Fixed text with no builder on the cached
layer, so a mode flip never rewrites the cached prefix. Headless runs,
scheduled tasks, agents and the on-device prompt build their own system
prompts and never read this one.

The theme half needs no per-request text: diagram and chart cards style
themselves from the app's live tokens (src/components/chat/VizCard.tsx), and
the HTML page card adds the same tokens as --ws-* variables
(src/utils/artifactTheme.ts). That holds for cards in the chat only. A file
the model saves is opened outside the app, where none of those tokens exist:
a run that applied the card rule to a saved page shipped it grey, in the
browser's default serif font. So the look of a saved file is the
model's own to design, and the text says to open it and look at it.
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
    "none when the user asks for text only, or when the picture would only "
    "repeat one sentence. Diagrams and charts made with create_visual and create_chart take "
    "the user's active Whisper theme and light or dark mode on their own, so set no colors "
    "or fonts on those. An HTML card in the chat gets that theme as --ws-* variables to "
    "build on. Everything else you make to be looked at (an HTML page, a 3D scene, "
    "an animation, an image, a slide) is yours to design. Choose its colors, type, "
    "materials, light, camera and motion yourself, to fit the subject and what the user "
    "asked for. A file you save is opened outside this app, so it carries its own complete "
    "look and inherits nothing. Before you say a visual piece is done, open it, look at it "
    "from the views that matter, and fix what does not look right."
)
