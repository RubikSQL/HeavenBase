"""Tiny standards-owned stdio MCP server for dashboard adapter tests."""

from fastmcp import FastMCP

SERVER = FastMCP("fake-mcp")


@SERVER.tool(annotations={"readOnlyHint": True})
def echo(text: str) -> str:
    """Echo the given text back."""
    return f"echo: {text}"


@SERVER.tool
def blowup() -> str:
    """Always fail."""
    raise RuntimeError("kaboom")


if __name__ == "__main__":
    SERVER.run(transport="stdio", show_banner=False)
