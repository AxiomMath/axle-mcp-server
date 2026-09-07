# Deploying mcp.axiommath.ai

Cloud Run service `axle-mcp`, project `axle-mcp-server`, region `us-central1`,
built from source. `mcp.axiommath.ai` is a domain mapping onto it.

```bash
gcloud auth login
gcloud run deploy axle-mcp --source . --project axle-mcp-server --region us-central1 \
  --set-secrets AXLE_MCP_TOKEN_SECRET=axle-mcp-token-secret:latest \
  --set-env-vars AXLE_MCP_PUBLIC_URL=https://mcp.axiommath.ai
```

Then check:

```bash
curl -s https://mcp.axiommath.ai/            # "token_secret": "configured"
curl -si -X POST https://mcp.axiommath.ai/mcp | head -5   # 401 with WWW-Authenticate
```

`AXLE_MCP_TOKEN_SECRET` (Secret Manager) encrypts the sign-in tokens; rotating
it signs every user out. Add `AXLE_MCP_ALLOW_ANONYMOUS=1` to `--set-env-vars`
to also serve requests with no credentials, as the server did before 0.4.0.
