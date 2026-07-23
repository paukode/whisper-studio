---
name: web_search
description: Searches the web via the Tavily API and returns up to 5 results, each with title, URL, and a short content snippet. Use for current events, prices, versions, docs, and any fact that may postdate training. Results are snippets only; follow up with web_fetch on the most promising URL when full page content is needed. Requires a Tavily API key (app Settings or the TAVILY_API_KEY environment variable); without one the skill explains the fallback options instead of returning results, so a non-result response means setup is needed, not that nothing was found. Do not use when the user already supplied the exact URL (use web_fetch) or for questions answerable from the conversation.
triggers: search, search the web, google, look up, find online, latest, news, weather, price, stock, current version
executor: web_search
input_schema:
  query:
    type: string
    required: true
    description: "The search query."
---

# Web Search

Executor-backed tool. This body is documentation for the Skills panel; the model
sees only the frontmatter description and input_schema. Behavior at runtime:

- Calls the Tavily API and returns up to 5 results, each as title, URL, and a short
  content snippet. Snippets only; use web_fetch on a result URL for full content.
- Requires a Tavily API key (app Settings, or the TAVILY_API_KEY environment
  variable). Without a key, if the Amazon Bedrock AgentCore browser is enabled the
  tool guides the model to drive that browser instead; otherwise it returns setup
  instructions. A non-result response means web search is not configured, not that
  there were no matches.

For a URL the user already provided, use web_fetch instead.
