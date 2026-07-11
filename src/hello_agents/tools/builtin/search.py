"""Search tool - lightweight web search built-in tool for the HelloAgents base.

Uses a best-effort public endpoint (DuckDuckGo HTML) with no API key, and
degrades gracefully when the network is unavailable. This is a base-framework
utility tool; the career agent uses dedicated job-source tools instead.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from ..base import Tool, ToolParameter

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class SearchTool(Tool):
    """Return a short list of web results for a query."""

    def __init__(self, max_results: int = 5):
        super().__init__(
            name="search",
            description="Search the web and return concise text results.",
        )
        self.max_results = max_results

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="query",
                type="string",
                description="Search query.",
                required=True,
            )
        ]

    def run(self, parameters: Dict[str, Any]) -> str:
        query = str(parameters.get("query") or parameters.get("input") or "").strip()
        if not query:
            return "Error: empty query"
        try:
            url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
            with urlopen(Request(url, headers={"User-Agent": _UA}), timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
        except Exception as exc:  # noqa: BLE001
            return f"Error: search failed ({exc})"
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)
        cleaned = [re.sub(r"<[^>]+>", "", s).strip() for s in snippets]
        cleaned = [c for c in cleaned if c][: self.max_results]
        if not cleaned:
            return "No results."
        return "\n".join(f"{i}. {c}" for i, c in enumerate(cleaned, 1))
