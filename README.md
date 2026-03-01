# AXLE MCP Server

A [Model Context
Protocol](https://modelcontextprotocol.io/docs/getting-started/intro) server for
[Axiom Lean Engine](https://axle.axiommath.ai) — exposes Lean verification and
manipulation tools to AI agents.


## Setup

1. Create a free API key:
   [https://axle.axiommath.ai/app/console](https://axle.axiommath.ai/app/console).

2. Add the MCP server to your client:

**Claude Code:**

Replace `your_api_key_here` with the API key you created in step 1:
```bash
claude mcp add axle -e AXLE_API_KEY=your_api_key_here -- uvx --from axiom-axle-mcp axle-mcp-server
```

**Other MCP clients (Cursor, Windsurf, Claude Desktop, VS Code, Cline, etc.):**

Add the following to your client's MCP config file. Replace `your_api_key_here`
with the API key you created in step 1:
```json
{
  "mcpServers": {
    "axle": {
      "command": "uvx",
      "args": ["--from", "axiom-axle-mcp", "axle-mcp-server"],
      "env": {
        "AXLE_API_KEY": "your_api_key_here"
      }
    }
  }
}
```


## Usage

With the MCP server installed, agents can check and fix Lean code directly:

> **You:** Can you check if this proof compiles?
>
> ```lean
> theorem add_comm (a b : Nat) : a + b = b + a := by
>   ring
> ```

Ask your agent what AXLE MCP can do.
