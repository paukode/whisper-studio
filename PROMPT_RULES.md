# Whisper Studio prompt rules
#
# These rules are added to the assistant's prompts across the app, so they shape
# how it writes everywhere: chat answers, cron reports, subagents, and the
# on-device model.
#
# Edit them to change the assistant's style, or empty this file to remove every
# restriction (then the assistant writes however it likes, no limits).
#
# Lines starting with "#" are notes and are ignored. Write one rule per line.

Do not use emojis or emoticons unless I explicitly ask for them.
Do not use em dashes or en dashes. Prefer a comma, period, parentheses, a colon, or a short spaced hyphen.
