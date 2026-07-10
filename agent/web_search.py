"""
agent/web_search.py

Reusable helper that runs Claude with the native `web_search` server tool
and returns a parsed JSON object.

Using Claude's server-side web search (rather than training knowledge) means:
  - Next-edition checks find dates that were announced *after* the model's cutoff
  - CFP checks reflect the live, current state of a conference's call for papers

The web_search tool is executed by Anthropic's servers; Claude issues one or
more searches, reads the results, and then produces a final answer. We instruct
it to finish with a single JSON object which we extract and parse.
"""

import json
import logging
import os
import re
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

_client: Optional[anthropic.Anthropic] = None

# Cap searches per request to control cost/latency
DEFAULT_MAX_USES = 4


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY is not set.")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def _extract_json(text: str) -> Optional[dict]:
    """
    Extract the first JSON object from a block of text.
    Handles markdown code fences and surrounding prose.
    """
    if not text:
        return None

    # Strip code fences
    cleaned = text.strip()
    if "```" in cleaned:
        # Grab content between the first pair of fences
        parts = cleaned.split("```")
        for part in parts:
            p = part.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                cleaned = p
                break

    # Find the outermost JSON object
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None

    candidate = cleaned[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # Last resort: try a non-greedy regex for a simple object
        match = re.search(r"\{.*\}", candidate, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
    return None


def search_and_extract(
    prompt: str,
    max_searches: int = DEFAULT_MAX_USES,
    max_tokens: int = 1024,
    model: str = "claude-sonnet-4-5",
) -> Optional[dict]:
    """
    Run Claude with the web_search tool against `prompt`, then extract and
    return the JSON object from its final response.

    The prompt MUST instruct Claude to:
      1. Search the web thoroughly
      2. End its reply with a single JSON object matching a described schema

    Returns the parsed dict, or None on failure.
    """
    client = _get_client()

    tools = [{
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": max_searches,
    }]

    try:
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            tools=tools,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as e:
        logger.error(f"Web search API error: {e}")
        return None

    # The final answer is the concatenation of all text blocks in the response
    text_parts = []
    for block in message.content:
        if getattr(block, "type", None) == "text":
            text_parts.append(block.text)

    final_text = "\n".join(text_parts).strip()
    if not final_text:
        logger.warning("Web search returned no text output")
        return None

    result = _extract_json(final_text)
    if result is None:
        logger.warning(f"Could not extract JSON from web search response: {final_text[:300]}")
    return result
