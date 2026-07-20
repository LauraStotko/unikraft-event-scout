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

# Generous defaults so Claude can finish searching AND emit the full JSON.
# Truncation (max_tokens cutoff) and search-budget exhaustion were the two main
# causes of "Could not extract JSON" failures, so both are raised here.
DEFAULT_MAX_USES = 6
DEFAULT_MAX_TOKENS = 4096


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY is not set.")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def _closers_for(fragment: str) -> str:
    """
    Return the correct sequence of closing brackets for `fragment`, respecting
    nesting order (innermost first). Ignores brackets inside strings.
    """
    stack = []
    in_str = False
    escape = False
    for ch in fragment:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch == "}" and stack and stack[-1] == "{":
            stack.pop()
        elif ch == "]" and stack and stack[-1] == "[":
            stack.pop()
    return "".join("}" if c == "{" else "]" for c in reversed(stack))


def _close_and_load(fragment: str) -> Optional[dict]:
    """Balance any open strings/brackets on `fragment` and try to json.loads it."""
    frag = fragment.rstrip().rstrip(",")
    # If inside an unterminated string, cut back to before it opened
    if frag.count('"') % 2 == 1:
        frag = frag[:frag.rfind('"')].rstrip().rstrip(",")
    # A trailing "key": with no value — drop the dangling key
    frag = re.sub(r',?\s*"[^"]*"\s*:\s*$', "", frag).rstrip().rstrip(",")
    repaired = frag + _closers_for(frag)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        return None


def _repair_truncated_json(candidate: str) -> Optional[dict]:
    """
    Parse JSON cut off mid-output (max_tokens hit). Progressively drops the
    trailing incomplete portion — first the dangling field, then whole elements
    from the end — closing brackets each time until it parses.
    """
    s = candidate.strip()

    # First attempt: just balance/close what we have (drops a dangling field)
    result = _close_and_load(s)
    if result is not None:
        return result

    # Otherwise drop trailing elements one comma at a time and retry
    work = s
    for _ in range(200):
        cut = work.rfind(",")
        if cut == -1:
            break
        work = work[:cut]
        result = _close_and_load(work)
        if result is not None:
            return result
    return None


def _extract_json(text: str) -> Optional[dict]:
    """
    Extract a JSON object from a block of text, robust to:
      - markdown ```json fences (including ones with no closing fence)
      - surrounding prose before/after the JSON
      - JSON truncated mid-output by a token limit
    """
    if not text:
        return None

    cleaned = text.strip()

    # If there's a ```json fence, take everything after the LAST one — that's
    # almost always where the final answer lives.
    if "```" in cleaned:
        # Prefer content following a ```json marker
        m = re.split(r"```(?:json)?", cleaned)
        # Pick the longest fragment that looks like it contains an object
        candidates = [frag for frag in m if "{" in frag]
        if candidates:
            cleaned = max(candidates, key=len).strip()

    start = cleaned.find("{")
    if start == -1:
        return None
    cleaned = cleaned[start:]

    # First try: well-formed object (find the matching outer brace)
    end = cleaned.rfind("}")
    if end != -1:
        try:
            return json.loads(cleaned[:end + 1])
        except json.JSONDecodeError:
            pass

    # Second try: repair a truncated / unbalanced object
    return _repair_truncated_json(cleaned)


def search_and_extract(
    prompt: str,
    max_searches: int = DEFAULT_MAX_USES,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    model: str = "claude-sonnet-4-5",
) -> Optional[dict]:
    """
    Run Claude with the web_search tool against `prompt`, then extract and
    return the JSON object from its final response.

    The prompt MUST instruct Claude to output a single JSON object.
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

    # Warn if the model was cut off — helps diagnose future issues
    if getattr(message, "stop_reason", None) == "max_tokens":
        logger.warning("Web search response hit max_tokens; attempting to salvage partial JSON.")

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
