# PRD: DeepWiki Open — Self-Hosted MCP Integration

**Version**: 1.2 (Final — Consensus Reviewed)
**Date**: 2026-03-15
**Status**: Approved — In Execution
**PAL Planner continuation_id**: 36fbd381-6ada-448a-ae8d-b27816149456
**PAL Consensus continuation_id**: f7896c50-8bd6-412d-8d1d-b3f1780bd945

---

## 1. Overview

### 1.1 Problem Statement
The hosted DeepWiki MCP at `mcp.deepwiki.com` provides AI-powered repository analysis for Claude Code, but is limited to public repositories, offers no customization, and sends code data to Cognition's servers.

### 1.2 Solution
Deploy `RichelynScott/deepwiki-open` (fork of `AsyncFuncAI/deepwiki-open`) as a Docker container with a FastMCP stream-aggregator wrapper exposing 6 MCP tools via stdio transport.

### 1.3 Success Criteria
| Criterion | Measurement |
|-----------|-------------|
| MCP tools discoverable | 6 tools visible in Claude Code |
| Ask questions | Query a public repo via self-hosted |
| Private repo support | Analyze a private GitHub repo with token |
| Local repo analysis | Analyze local filesystem repo |
| Coexistence | Both `deepwiki` and `deepwiki-local` work simultaneously |
| Zero upstream mods | `git diff origin/main` shows no changes to upstream files |
| Documentation complete | CLAUDE.md, FYI.md, memory entries created |

---

## 2. Architecture

```
Claude Code CLI (WSL2)
  |
  +-- deepwiki (hosted MCP)
  |   +-- HTTP -> https://mcp.deepwiki.com/mcp
  |
  +-- deepwiki-local (self-hosted MCP)
      +-- stdio -> python mcp_wrapper.py
          |
          +-- httpx -> http://localhost:8001 (Docker)
              |
              Docker Container (deepwiki-open, upstream unmodified)
              +-- FastAPI Backend (:8001)
              +-- Next.js Frontend (:3000)
              +-- Volumes:
                  +-- ~/.adalflow (persistent)
                  +-- ~/Proj (read-only)
                  +-- ~/MCPs (read-only)
```

---

## 3. MCP Tools (6)

| Tool | Backend Endpoint | Timeout | Description |
|------|-----------------|---------|-------------|
| `ask_question` | POST /chat/completions/stream | 600s | Ask about any repo using RAG |
| `read_wiki_structure` | GET /api/wiki_cache | 30s | Get wiki table of contents |
| `read_wiki_contents` | GET /api/wiki_cache | 30s | Read full generated wiki |
| `list_projects` | GET /api/processed_projects | 10s | List analyzed repos |
| `analyze_local_repo` | GET /local_repo/structure + POST /chat/completions/stream | 600s | Analyze local repo |
| `health_check` | GET /health + GET /models/config | 10s | Check container health |

---

## 4. Security Controls

| Control | Implementation |
|---------|---------------|
| Port binding | `127.0.0.1` only |
| Volume mounts | `:ro` for project dirs |
| Container hardening | `no-new-privileges` |
| API keys | `.env` gitignored |
| Path translation | Explicit allowlist + symlink check |
| SSE validation | Content-Type check before parsing |
| Overflow | Typed ToolError at 10MB limit |

---

## 5. Streaming Contract

**Pre-stream (retryable)**: Connection, request, headers, status validation
**Stream (NOT retryable)**: SSE chunks collected, 30s idle timeout, 600s max, 10MB cap
**Post-stream**: Chunks joined, returned as complete MCP response

---

## 6. Consensus Review Results

| Model | Stance | Score | Key Feedback |
|-------|--------|-------|-------------|
| GPT-5.4-Pro | Critique | N/A | Stream aggregator OK for MCP; validate SSE handling |
| Claude Sonnet 4.6 | Adversarial | 8/10 | Symlink check, ToolError overflow, pin upstream |
| DeepSeek | Neutral | 8/10 | Docker hardening, observability, path testing |

---

## 7. Upstream Pin

**Commit**: `4c6a1f7`
**Description**: Fix running Ollama in docker. Fixing embedder configuration.
**Review cadence**: Quarterly check upstream changelog.
