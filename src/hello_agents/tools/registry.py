"""Registry for tools available to controlled execution."""

from typing import Optional
from .base import Tool


class ToolRegistry:
    """Store Tool objects without providing an execution bypass."""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register_tool(self, tool: Tool) -> None:
        """Register or replace a Tool by name."""
        self._tools[tool.name] = tool

    def get_tool(self, name: str) -> Optional[Tool]:
        """Return a registered Tool."""
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        """List registered tool names."""
        return list(self._tools)

    def get_all_tools(self) -> list[Tool]:
        """Return registered Tool objects."""
        return list(self._tools.values())

    def describe_tools(self) -> str:
        """Render descriptions and JSON parameter schemas for a planner."""
        if not self._tools:
            return "No tools are registered."
        return "\n".join(
            f"- {tool.name}: {tool.description}; "
            f"parameters={tool.to_openai_schema()['function']['parameters']}"
            for tool in self._tools.values()
        )

    def unregister(self, name: str) -> bool:
        """Remove one tool without executing it."""
        return self._tools.pop(name, None) is not None

    def clear(self) -> None:
        """Remove all registered tools."""
        self._tools.clear()
