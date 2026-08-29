"""Intent gating for index grounding: when does a chat message deserve retrieval?

Grounding used to fire on every non-empty message. A contentless greeting
("hey") embeds as cosine noise, and when the word literally appears anywhere
in the corpus (an old meeting transcript, say) it rides the floor-exempt BM25
keyword leg straight into the prompt. The injected block then instructs the
model to answer from the passages, and with no real question to attach to, a
small local model treats the injection itself as the task and invents one.

Design rule: skip retrieval ONLY when the message is confidently contentless.
The stoplist holds English function words plus greeting/acknowledgment
vocabulary; any token outside it (jargon, an identifier, a name, another
language) counts as content, so when in doubt retrieval runs.
"""

import re

# English function words, conversational smalltalk, and apostrophe-collapsed
# contractions ("don't" tokenizes as "dont"). Lowercase-only; matching is
# case-insensitive via lowercased tokens.
_NON_CONTENT = frozenset(
    """
    a about after again all also am an and any are as at be because been but by
    can could did do does doing done for from get got had has have he her here
    hers him his how i if in into is it its just like me mine my no nor not now
    of off on once only or other our ours out over own she so some such than
    that the their theirs them then there these they this those through to too
    under until up very was we were what when where which while who whom why
    will with would you your yours
    dont cant wont im ive ill id youre youve youd thats whats lets isnt arent
    wasnt werent didnt doesnt hasnt havent couldnt shouldnt wouldnt
    hey hi hello heya hiya yo sup howdy greetings
    thanks thank thx ty cheers
    ok okay kk fine cool nice great awesome perfect sweet neat lovely
    yes yeah yep yup sure nope nah
    please pls sorry oops
    good morning afternoon evening night day
    bye goodbye later ciao
    hmm hm huh eh oh ah wow lol haha hehe
    """.split()
)

_TOKEN_RE = re.compile(r"[^\W_]+")

# Explicit "@index" mention: forces grounding for the turn, then vanishes from
# the prompt. Preceded by start-of-string or whitespace so an address like
# foo@index.com never matches; the trailing \b keeps @indexes/@indexing
# untouched. An optional colon tolerates the "@index: question" spelling.
_INDEX_TRIGGER_RE = re.compile(r"(?:^|(?<=\s))@index\b:?", re.IGNORECASE)

# Explicit "@docs" mention: this turn answers from the app's own manual (the
# bundled documentation site) instead of the workspace indexes. Same matching
# rules as @index.
_DOCS_TRIGGER_RE = re.compile(r"(?:^|(?<=\s))@docs\b:?", re.IGNORECASE)


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.replace("'", "").replace("’", "").lower())


def has_content_word(text: str) -> bool:
    """True when at least one token is content-bearing (not a function word,
    greeting, or filler). Unknown tokens count as content by design."""
    return any(t not in _NON_CONTENT for t in _tokens(text))


def should_ground(question: str) -> bool:
    """Should this chat message trigger automatic index retrieval?

    Currently the content-word test alone; a separate name so the chat route
    reads as policy and the policy can grow without touching call sites.
    """
    return has_content_word(question)


def _extract_trigger(pattern: re.Pattern, question: str) -> tuple[bool, str]:
    if not pattern.search(question):
        return False, question
    stripped = pattern.sub("", question)
    return True, re.sub(r"[ \t]{2,}", " ", stripped).strip()


def extract_index_trigger(question: str) -> tuple[bool, str]:
    """Detect and strip an explicit ``@index`` mention.

    Returns ``(forced, question_without_marker)``. The question comes back
    verbatim when no marker is present; when one is, every occurrence is
    removed and the leftover double spaces collapsed (newlines preserved).
    """
    return _extract_trigger(_INDEX_TRIGGER_RE, question)


def extract_docs_trigger(question: str) -> tuple[bool, str]:
    """Detect and strip an explicit ``@docs`` mention (ask the app manual).
    Same contract as ``extract_index_trigger``."""
    return _extract_trigger(_DOCS_TRIGGER_RE, question)
