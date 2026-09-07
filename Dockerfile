FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY axle_mcp_server ./axle_mcp_server

RUN pip install --no-cache-dir ".[http]"

ENV PORT=8080
EXPOSE 8080

# Set AXLE_MCP_TOKEN_SECRET on the deployment; see README "Self-hosting".
CMD ["axle-mcp-server", "--http"]
