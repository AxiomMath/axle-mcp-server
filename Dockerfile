FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY axle_mcp_server ./axle_mcp_server

RUN pip install --no-cache-dir ".[http]"

ENV PORT=8080
EXPOSE 8080

# Runtime configuration (set these on the deployment, not here):
#   AXLE_MCP_TOKEN_SECRET  Required in production. Long random string used to seal the
#                          OAuth tokens. Unset => random per process: tokens break on
#                          every restart and across instances.
#   AXLE_MCP_PUBLIC_URL    Public origin, e.g. https://mcp.axiommath.ai. Optional;
#                          derived from X-Forwarded-Proto/Host when unset.
#   AXLE_API_URL           Upstream AXLE API (default https://axle.axiommath.ai).
#   AXLE_MCP_ALLOW_ANONYMOUS=1  Also serve requests with no credentials (old behavior).
CMD ["axle-mcp-server", "--http"]
