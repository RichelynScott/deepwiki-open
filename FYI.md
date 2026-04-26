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

## 2026-03-18 — Local Directory Support + Metadata + Streaming Fixes
### What: Major feature additions and bug fixes across 4 commits
### Why: Original wrapper only worked with GitHub URLs; user needed local directory analysis
### How: Fixed stream parsing (DeepWiki sends plain text despite SSE Content-Type), fixed local path handling (raw path, not local:// prefix), added metadata footer to tool responses
### Impact: `ask_question` now accepts both URLs and local paths (`/home/riche/Proj/*`)

### Bug Fixes
1. **FastMCP 3.x API change**: Removed deprecated `description` kwarg from constructor
2. **SSE parsing**: DeepWiki sends plain text with `Content-Type: text/event-stream` — now auto-detects format per chunk instead of trusting header
3. **Local path handling**: Changed `local://container_path` to raw `container_path` — DeepWiki treats any non-http path as local

### Embedding Storage Analysis
- Format: AdalFlow `LocalDB` pickle → `Document` objects with 256-dim vectors
- Model: OpenAI `text-embedding-3-small` (truncated from 1536 to 256)
- Storage: `~/.adalflow/databases/{repo_name}.pkl` — one file per repo
- Cached after first query — subsequent queries only cost LLM call
- Cost: ~$0.02/1M tokens for embeddings (~$0.10/year at current usage)

### PAL Expert Analysis: Cost Optimization (Gemini 3.1 Pro)
- **Embeddings**: Keep OpenAI — $0.10/year is effectively free, zero server footprint
- **Vector store**: FAISS lacks CRUD/metadata filtering — LanceDB recommended as upgrade
- **LLM**: Gemini 2.5 Flash is cost-optimal at $0.075/1M input tokens
- **Ollama local**: NOT cheaper when factoring daemon RAM overhead

### Global Docs Updated
- 3 skills: research-methodologies, tool-guides, deep-research
- 3 docs: advanced-methodologies.md, MULTI_AGENT_ORCHESTRATION_SPEC.md, CLAUDE_DESKTOP_WSL_INTEGRATION.md
- MASTER_AGENT_DIRECTORY.md, agent-orchestration skill (prior session)

## 2026-04-25 — Cost Telemetry + Tool Bug Fixes + CLAUDE.md Rewrite (commit d43e678)
### What
Three independent improvements landed in one commit:
1. **D1 — Tool bug fixes**:
   - `health_check`: was reporting JSON wrapper keys (`providers, defaultProvider`) instead of actual provider IDs. Now iterates `data["providers"]` array and reports 7 providers + default.
   - `list_projects`: was returning "No projects analyzed" while 4 RAG caches existed in `~/.adalflow/databases/`. Now reports two sections: generated wikis (from `/api/processed_projects`) and RAG embedding caches (filesystem scan with sizes).
2. **D2-A — Cost estimator**:
   - `tiktoken==0.12.0` added to `requirements-mcp.txt`.
   - `cl100k_base` tokenizer for output (counted exactly) and question (exact). Input includes a 5K-token RAG-context floor.
   - `PRICE_TABLE_USD_PER_M` covers `gemini-2.5-{flash,flash-lite,pro}` and `gpt-{4o,4o-mini,5,5-mini}`. Unknown models fall to `$1.00/$4.00 per M` with a ⚠️ warning.
   - First-call embedding cost flagged in footer when `embeddings_cached=False` (range $0.01–$0.10, not included in per-call estimate).
3. **D3 — Per-call usage log**:
   - Every `ask_question` and `analyze_local_repo` appends a JSON line to `~/.deepwiki-local-usage.jsonl` with timestamp, source, provider, model, token estimates, cost, duration, cache state.
   - Grep-friendly weekly tally via jq.

### Why
User asked: "Why is there no token or cost measurements/estimates before costing? How can we improve this?" Root cause: DeepWiki upstream (`api/simple_chat.py`) streams plain model text and never emits a final SSE `usage` event, so the wrapper had no upstream signal to capture. Fix: do it locally with tiktoken + a price table.

### How
Wrapper-only changes; no upstream files touched. Estimator runs after `_stream_response` returns; metadata footer extended; jsonl log appended best-effort (never fails the call).

### Impact
- Every paid call now shows estimated cost in metadata footer.
- Lifetime/weekly spend tracking via `jq -s 'map(.cost_total_usd) | add' ~/.deepwiki-local-usage.jsonl`.
- Two latent UX bugs fixed (health_check, list_projects).
- CLAUDE.md fully rewritten — adds customization-seam hard rule, mounts table with `:rw` reasons, security incl. WSL2 NAT note, known issues, history, ops including reconnect rule.

### Known limits (intentional)
- No pre-call dry-run preview (would require mirroring DeepWiki retriever).
- Input estimate is a floor (real RAG retrieval depth unobservable).
- Estimates may drift ±20% from provider billing — verify dashboards for spend that matters.

### Related
- Commit `d43e678`
- Global: `~/.claude/MASTER_DIRECTORIES/MASTER_MCP_DIRECTORY.md` line 42 updated to reflect cost telemetry and enabled state
