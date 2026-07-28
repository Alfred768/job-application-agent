"""Small public web-search example Tool."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any, Dict, List
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from hello_agents.core.contracts import ToolEffect

from ..base import Tool, ToolParameter


_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
)


class _SearchResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[str] = []
        self._capture_depth = 0
        self._parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        classes = dict(attrs).get("class", "") or ""
        if "result__snippet" in classes or "result__a" in classes:
            self._capture_depth = 1
            self._parts = []
        elif self._capture_depth:
            self._capture_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if not self._capture_depth:
            return
        self._capture_depth -= 1
        if self._capture_depth == 0:
            text = " ".join(" ".join(self._parts).split())
            if text:
                self.results.append(text)
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_depth:
            self._parts.append(data)


class SearchTool(Tool):
    """Search a public endpoint and return concise text results."""

    def __init__(self, max_results: int = 5) -> None:
        super().__init__(
            name="search",
            description="Search the public web for a query.",
            effect=ToolEffect.READ,
        )
        self.max_results = max_results

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="query",
                type="string",
                description="Public web search query.",
            )
        ]

    def run(self, parameters: Dict[str, Any]) -> str:
        query = str(
            parameters.get("query")
            or parameters.get("input")
            or ""
        ).strip()
        if not query:
            return "Error: empty query"
        request = Request(
            f"https://duckduckgo.com/html/?q={quote_plus(query)}",
            headers={"User-Agent": _USER_AGENT},
        )
        try:
            with urlopen(request, timeout=15) as response:
                html = response.read().decode("utf-8", errors="ignore")
        except Exception as exc:  # noqa: BLE001
            return f"Error: search failed ({type(exc).__name__}: {exc})"
        parser = _SearchResultParser()
        parser.feed(html)
        unique = list(dict.fromkeys(parser.results))[: self.max_results]
        if not unique:
            return "No results."
        return "\n".join(
            f"{index}. {result}"
            for index, result in enumerate(unique, start=1)
        )
