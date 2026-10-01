"""Sanitize SDK validation/runtime failures before MCP logging or client responses."""
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent


class SafeMCPServer(MCPServer):
    async def _handle_call_tool(self, ctx, params):
        if self._tool_manager.get_tool(params.name) is None:
            # The SDK logger otherwise includes the peer-controlled tool name.
            return CallToolResult(
                content=[TextContent(type="text", text="Unknown tool.")], is_error=True)
        return await super()._handle_call_tool(ctx, params)

    async def call_tool(self, name, arguments, context=None):
        try:
            return await super().call_tool(name, arguments, context)
        except Exception:
            # SDK validation errors include rejected values and sometimes dict
            # keys in locations. A schema-valid error message still must not
            # echo a secret. Suppress the original cause and traceback chain.
            raise ToolError("Tool request rejected; check the schema and privacy requirements.") from None
