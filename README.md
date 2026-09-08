# AXLE MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io/docs/getting-started/intro)
server for [Axiom Lean Engine](https://axle.axiommath.ai) — exposes Lean
verification and manipulation tools to AI agents.

![](demo.gif)

## Installation

1. Create a free API key: [https://axle.axiommath.ai/app/console](https://axle.axiommath.ai/app/console).
2. Connect your client:

### Claude (web, desktop, mobile)

1. Open [Customize → Connectors](https://claude.ai/customize/connectors) → **Add** → **Add custom connector**.
2. Name: `Axle`. Remote MCP server URL: `https://mcp.axiommath.ai/mcp`. Click **Add**.
3. Click **Add** again to accept the default client settings.
4. Click **Connect** and paste your API key on the sign-in page.
5. In a chat, open the **+** menu → **Connectors** and switch **Axle** on.

### ChatGPT

Needs a paid plan and Developer mode (**Settings → Security and login**).

1. Open [ChatGPT Plugins](https://chatgpt.com/plugins) → **+**.
2. Name: `Axle`. MCP server URL: `https://mcp.axiommath.ai/mcp`. Authentication: **OAuth**.
3. Create it and paste your API key on the sign-in page.
4. In a chat, add Axle from the **+** → **Developer mode** menu.

### Claude Code

```bash
claude mcp add --transport http axle https://mcp.axiommath.ai/mcp
```

Then run `/mcp` and paste your API key on the page that opens. To skip the
browser, pass the key directly:

```bash
claude mcp add --transport http axle https://mcp.axiommath.ai/mcp \
  --header "Authorization: Bearer your_api_key_here"
```

### Other MCP clients (Cursor, Windsurf, VS Code, Cline, ...)

```json
{
  "mcpServers": {
    "axle": {
      "type": "http",
      "url": "https://mcp.axiommath.ai/mcp",
      "headers": {
        "Authorization": "Bearer your_api_key_here"
      }
    }
  }
}
```

To run the server locally instead (enables `file_uri`, which reads Lean files
from disk):

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

## Tools

Most tools are generated from the AXLE API's `/v1/endpoints` — `verify_proof`,
`check`, `merge`, `sorry2lemma` and friends. Alongside them the server provides:

| Tool | Purpose |
| --- | --- |
| `read_docs` | Read the AXLE documentation. Call with no arguments for the page index, then `page="verify_proof"` for one page. |
| `list_environments` | List the available Lean environments. |
| `share_url` | Turn a prior call's `request_id` into a permanent shareable webapp URL. |
| `read_share_url` | Read back the inputs and result behind a share URL. |
