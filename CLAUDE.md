# DeepWiki Open — Self-Hosted MCP Integration

## Project Purpose
Self-hosted DeepWiki instance providing AI-powered repository analysis via MCP tools.
Runs in Docker, exposes 6 MCP tools via `mcp_wrapper.py` (FastMCP stream aggregator).

## Architecture
```
Claude Code → stdio → mcp_wrapper.py → httpx → localhost:8001 → Docker (DeepWiki)
```

- **Upstream**: AsyncFuncAI/deepwiki-open (pinned at commit 4c6a1f7)
- **Fork**: RichelynScott/deepwiki-open
- **Docker**: docker-compose.yml (upstream) + docker-compose.override.yml (ours)
- **MCP**: mcp_wrapper.py (FastMCP wrapper, 6 tools)
- **Storage**: ~/.adalflow/ (repos, FAISS indices, wiki cache)

## Key Rules
1. **NEVER modify upstream files** (api/, src/, Dockerfile, docker-compose.yml, package.json)
2. All customizations go in: `mcp_wrapper.py`, `docker-compose.override.yml`, `.env`, `docs/`
3. Keep MCP tool names matching hosted DeepWiki MCP where applicable
4. `.env` never committed — use `.env.sample` for templates
5. Pin upstream to known-good commit SHA (currently `4c6a1f7`)

## MCP Tools (6)
| Tool | Description |
|------|-------------|
| `ask_question` | Ask a question about any repo using RAG |
| `read_wiki_structure` | Get wiki table of contents |
| `read_wiki_contents` | Read full generated wiki |
| `list_projects` | List all analyzed/cached repos |
| `analyze_local_repo` | Analyze a local filesystem repo |
| `health_check` | Check if Docker container is running |

## Running
```bash
# Start DeepWiki container
docker compose up -d

# Stop
docker compose down

# Logs
docker compose logs -f deepwiki

# Health check
curl http://localhost:8001/health

# Test MCP wrapper standalone
.venv/bin/python mcp_wrapper.py
```

## Volume Mounts (Docker)
| Host Path | Container Path | Access |
|-----------|---------------|--------|
| `~/.adalflow` | `/root/.adalflow` | read-write |
| `/home/riche/Proj` | `/host/projects` | read-only |
| `/home/riche/MCPs` | `/host/mcps` | read-only |

## Security
- Ports bound to `127.0.0.1` only (not `0.0.0.0`)
- `security_opt: no-new-privileges` on container
- Read-only volume mounts for project directories
- Path translation uses explicit allowlist with symlink check
- Content-Type validation before SSE parsing
