#!/usr/bin/env python3
"""
DEPRECATED: This script patched the old mcp-memory-service server_impl.py
which has been replaced by b12_mcp_server.py. Kept for reference only.

Original purpose:
    Disable MCP SDK input validation in server_impl.py by changing
    @self.server.call_tool() to @self.server.call_tool(validate_input=False)
"""

import sys


def main():
    print("  [DEPRECATED] This script is no longer needed.")
    print("  B12 now uses b12_mcp_server.py instead of mcp-memory-service.")
    sys.exit(0)


if __name__ == "__main__":
    main()
