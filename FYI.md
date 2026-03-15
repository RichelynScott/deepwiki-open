# FYI — DeepWiki Open Self-Hosted MCP Integration

## 2026-03-15 — Project Initialized
### What: Self-hosted DeepWiki Open MCP integration created
### Why: Hosted DeepWiki MCP (mcp.deepwiki.com) lacks private repo support, local repo analysis, custom AI providers, and data sovereignty
### How: Fork of AsyncFuncAI/deepwiki-open deployed in Docker with FastMCP stream-aggregator wrapper (mcp_wrapper.py)
### Impact: Claude Code gains 6 new MCP tools for private/local repo analysis via `deepwiki-local`

### Architecture Decision: Stream Aggregator (not fastapi-mcp)
- Initially considered `fastapi-mcp` library (3 lines added to api/main.py)
- Pivoted to thin FastMCP wrapper after PAL thinkdeep + consensus review
- Reasons: zero upstream modification, clean tool naming, path translation, custom error handling
- Validated by multi-model consensus (Sonnet 4.6: 8/10, DeepSeek: 8/10)

### Key Design Decisions
1. **Location**: `/home/riche/MCPs/deepwiki-open/` (MCP server pattern, not Proj)
2. **Transport**: stdio (subprocess) — simplest for Claude Code
3. **HTTP client**: httpx (raw HTTP, not SDK) — precise streaming control
4. **Streaming**: Aggregator pattern — collect SSE chunks, return complete text
5. **Coexistence**: `deepwiki-local` alongside hosted `deepwiki` in .mcp.json
6. **Upstream pin**: Commit 4c6a1f7 (2026-03-15 fork state)

### Security Improvements (from consensus review)
- Symlink traversal check in path translation
- Content-Type validation before SSE parsing
- Docker `no-new-privileges` security option
- Typed ToolError on response overflow (not silent truncation)
