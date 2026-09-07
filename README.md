# AXLE MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io/docs/getting-started/intro)
server for [Axiom Lean Engine (AXLE)](https://axle.axiommath.ai) — exposes Lean
verification and manipulation tools to Claude, ChatGPT and any other MCP client.

![](demo.gif)

A hosted instance runs at **`https://mcp.axiommath.ai/mcp`**. You authenticate
with your own AXLE API key, either on a sign-in page (Claude, ChatGPT, Claude
Code) or in a header (Claude Code, Cursor, VS Code, ...).

## 1. Get an API key

Create a free API key at
[https://axle.axiommath.ai/app/console](https://axle.axiommath.ai/app/console).

## 2. Connect your client

### Claude (web, desktop, mobile)

Custom connectors are available on Free, Pro, Max, Team and Enterprise plans.

1. Open [Customize → Connectors](https://claude.ai/customize/connectors).
2. Click **Add custom connector** (Team/Enterprise owners: **Organization
   settings → Connectors → Add → Custom → Web**).
3. Enter:
   - **Name:** `Axle`
   - **Remote MCP server URL:** `https://mcp.axiommath.ai/mcp`
   - Leave the OAuth client settings at their defaults.
4. Click **Add**, then **Connect**. Paste your AXLE API key on the sign-in page
   that opens and click **Connect**.
5. In a chat, open the **+** menu → **Connectors** and switch **Axle** on.

To change the key later, remove the connector and add it again.

### ChatGPT (web)

Custom MCP servers need a Plus, Pro, Business, Enterprise or Education account
and **Developer mode**.

1. In ChatGPT open **Settings → Security and login** and turn on **Developer
   mode**.
2. Go to [ChatGPT Plugins](https://chatgpt.com/plugins), click the **+** button
   and create a new connection:
   - **Name:** `Axle`
   - **Description:** `Lean 4 proof verification with Axiom Lean Engine`
   - **MCP server URL:** `https://mcp.axiommath.ai/mcp`
   - **Authentication:** **OAuth** (leave client ID / secret empty)
3. Create it, paste your API key on the sign-in page that opens and click
   **Connect**.
4. In a new conversation, add Axle from the tools menu (**+** → **Developer
   mode** → select **Axle**).

### Claude Code

Remote server with browser sign-in (recommended):

```bash
claude mcp add --transport http axle https://mcp.axiommath.ai/mcp
```

Then run `/mcp` inside Claude Code (or `claude mcp login axle`) and paste your
API key on the page that opens. Add `-s user` to use it in every project.

Remote server with the key in a header (no browser):

```bash
claude mcp add --transport http axle https://mcp.axiommath.ai/mcp \
  --header "Authorization: Bearer your_api_key_here"
```

Local stdio server (also enables `file_uri`, so tools can read Lean files from
disk):

```bash
claude mcp add axle -e AXLE_API_KEY=your_api_key_here -- uvx --from axiom-axle-mcp axle-mcp-server
```

### Cursor, Windsurf, VS Code, Claude Desktop (config file), Cline, ...

Put your key in the header, or omit the header and let the client run the OAuth
sign-in if it supports it:

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

Or run the server locally over stdio:

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

All tools except `share_url` are marked read-only.

## How authentication works

Claude and ChatGPT can only attach an OAuth token to a remote MCP server, so in
HTTP mode the server is its own OAuth 2.1 authorization server (DCR or CIMD
client registration, PKCE S256). The sign-in page takes your AXLE API key,
verifies it against AXLE and seals it into the access token (1 hour, refreshed
by the client) and refresh token (90 days); each MCP request unseals the key
and forwards it to AXLE. A raw key in `Authorization: Bearer <key>` is also
accepted. Nothing is stored server-side.

## Self-hosting

```bash
docker build -t axle-mcp .
docker run -p 8080:8080 \
  -e AXLE_MCP_TOKEN_SECRET="$(openssl rand -hex 32)" \
  -e AXLE_MCP_PUBLIC_URL=https://mcp.example.com \
  axle-mcp
```

| Variable | Purpose |
| --- | --- |
| `AXLE_MCP_TOKEN_SECRET` | **Required in production.** Seals the OAuth tokens; same value on every instance. Unset ⇒ random per process, so tokens die on restart. |
| `AXLE_MCP_PUBLIC_URL` | Public origin, e.g. `https://mcp.example.com`. Default: derived from `X-Forwarded-Proto` / `Host`. |
| `AXLE_API_URL` | Upstream AXLE API. Default `https://axle.axiommath.ai`. |
| `AXLE_DEFAULT_ENVIRONMENT` | Override the default Lean environment (default: newest `lean-4.x.y`). |
| `AXLE_MCP_ALLOW_ANONYMOUS` | `1` to also serve requests with no credentials. Off by default. |
| `AXLE_API_KEY` | Key sent to AXLE in stdio mode (and for anonymous HTTP requests when enabled). |
| `PORT` | HTTP port (default 8080). |

`/mcp` must be reachable over public HTTPS without redirects.

## Development

```bash
uv sync --extra http --group dev
uv run pytest
uv run ruff check . && uv run mypy axle_mcp_server
AXLE_MCP_TOKEN_SECRET=dev uv run axle-mcp-server --http --port 8080
claude mcp add --transport http axle-dev http://127.0.0.1:8080/mcp && claude mcp login axle-dev
```
