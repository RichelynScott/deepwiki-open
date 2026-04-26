# DeepWiki Open — Self-Hosted MCP Integration

Self-hosted fork of [AsyncFuncAI/deepwiki-open](https://github.com/AsyncFuncAI/deepwiki-open) that serves AI-powered repository RAG over a local Docker container, exposed to Claude Code via a thin FastMCP stdio wrapper.

## Architecture

```
Claude Code (stdio) ─► mcp_wrapper.py ─► httpx ─► http://localhost:8001 ─► Docker(deepwiki)
                                                                            │
                                                                            ├─ /chat/completions/stream  (RAG Q&A)
                                                                            ├─ /local_repo/structure     (local-dir analysis)
                                                                            ├─ /api/processed_projects   (generated wikis)
                                                                            ├─ /api/wiki_cache           (wiki contents)
                                                                            ├─ /models/config            (provider list)
                                                                            └─ /health, /auth/status
```

| Layer | Files | Owner |
|---|---|---|
| **Upstream** (do not modify) | `api/`, `src/`, `Dockerfile`, `docker-compose.yml`, `package.json`, `next.config.ts` | AsyncFuncAI |
| **Our customizations** | `mcp_wrapper.py`, `docker-compose.override.yml`, `.env`, `requirements-mcp.txt`, `CLAUDE.md`, `FYI.md`, `docs/` | This fork |
| **State** | `~/.adalflow/databases/*.pkl` (RAG embeddings), `~/.adalflow/wikicache/` (generated wikis), `~/.adalflow/repos/` (cloned source), `api/logs/` (app logs), `~/.deepwiki-local-usage.jsonl` (per-call cost log) | Runtime |

- **Upstream effective state**: last merged at `05591ee` (LaTeX math PR #499, merged 2026-04-23 via merge commit `8437210`). Note: earlier docs cited `4c6a1f7` as the pin; that's stale — local `main` is 3 commits past it. Sync via `.fork-sync.yml`.
- **Upstream cadence**: ~4 commits / 6 weeks (active but slow). Check drift periodically:
  ```bash
  git fetch upstream && git log 05591ee..upstream/main --oneline
  ```
- **Fork**: RichelynScott/deepwiki-open.

## Customization Seams (the only places you may edit)

1. **`mcp_wrapper.py`** — FastMCP stdio server. 6 tools, httpx async, stream aggregator with size/time/idle limits, allowlisted host→container path translation, tiktoken-based cost estimator, `~/.deepwiki-local-usage.jsonl` log.
2. **`docker-compose.override.yml`** — additional volume mounts, `restart: unless-stopped`, `security_opt: no-new-privileges`. Compose merges (appends) ports — do NOT redeclare ports here.
3. **`.env`** — provider API keys (OPENAI, GOOGLE, GEMINI, OPENROUTER, AZURE, OLLAMA). Never commit. Template in `.env.sample`.
4. **`requirements-mcp.txt`** — wrapper-only deps (`fastmcp`, `httpx`, `httpx-sse`, `tiktoken`). Upstream deps live in `api/pyproject.toml` and `package.json` — leave alone.
5. **`docs/`**, **`FYI.md`**, **`CLAUDE.md`** — documentation. No upstream conflict surface.

**Hard rule:** any change to a file outside this seam list is a fork-divergence risk. Use `docker-compose.override.yml` to override container behavior; do NOT patch `Dockerfile`. Use `mcp_wrapper.py` to shape tool behavior; do NOT patch `api/simple_chat.py`.

## MCP Tools

| Tool | Purpose | Cost |
|---|---|---|
| `ask_question(repo_url, question, provider, model, language, token, force_refresh)` | RAG Q&A over a remote URL or absolute local path. Auto-detects local vs remote and translates path via allowlist. `force_refresh=True` invalidates cached embeddings. | LLM call + (first-time only) embedding generation. Estimate appended to response. |
| `analyze_local_repo(path, question, force_refresh)` | Same as `ask_question` for a host filesystem path. With `question=""`, returns repo structure only (free). | Same as `ask_question` when `question` set. Free for structure-only. |
| `read_wiki_structure(owner, repo, repo_type, language)` | TOC of a previously generated wiki. | Free (cache read). |
| `read_wiki_contents(owner, repo, repo_type, language, section)` | Full generated wiki text, optionally filtered to a section. | Free (cache read). |
| `list_projects()` | Lists generated wikis AND RAG embedding caches separately. A repo with embeddings but no wiki is still queryable. | Free. |
| `health_check()` | Container reachability, provider list, default provider, auth mode, API URL. | Free. |

## Citation-Contamination Guard (added 2026-04-25)

**Bug discovered**: DeepWiki's upstream file walker (`api/data_pipeline.py:153-380`, `glob.glob` recursive) embeds the **entire** repo tree by default — including ephemeral peer correspondence (`*_INBOX/`, `HANDOFF.md`, `FYI.md`), task-coordination scratch (`tasks/hermes-briefs/`), and `archive/`. The RAG layer then retrieves these as authoritative evidence, leading to circular citation contamination (an agent's own freshly-written prose cited back as repo ground truth). Reported by HERMES_CC_MGR_AKA_TEST_KING_MGR with concrete reproduction.

**Fix**: The wrapper now injects `excluded_dirs` and `excluded_files` into every `/chat/completions/stream` request via the upstream API's existing runtime knobs (`api/simple_chat.py:71-74`). Two layers of exclusion:

1. **Static list** (`EPHEMERAL_STATIC_EXCLUDED_DIRS` / `_FILES`): `archive`, `_archive`, `hermes-briefs`, `claude-briefs`, `logs`, `.cpm-logs`, `.claude`, `.hermes`, `.codex`; files: `HANDOFF.md`, `FYI.md`, `DECISIONS_LOG.md`, `RATE_LIMIT_TRIGGERED.md`, `.claude-session-name`, `.claude-current-model`.
2. **Dynamic discovery**: walks repo top-level + 1 deep, finds any folder ending in `_INBOX` (per-session inbox folders have variable names) and adds them to the excluded set.

**Important**: Existing cached `.pkl` files were generated WITHOUT this filter. To purge contamination from prior caches, call `ask_question(..., force_refresh=True)` once per repo.

**Limitation**: Upstream's matching is exact path-component (no glob support). The wrapper's static list covers common names; rare variants need adding to `EPHEMERAL_STATIC_EXCLUDED_*`.

## Cache Freshness

The wrapper warns when the `.pkl` is stale (any source file newer than DB mtime). The footer shows DB age and a 5-file sample of newer files. Pass `force_refresh=True` to regenerate (re-incurs embedding cost).

Walks up to 200 files, skipping `.git`, `node_modules`, `.venv`, `__pycache__`, `.next`, `dist`, `build`, `archive`, `.adalflow`, `.claude`, and dotfiles. Bound by `STALENESS_FILE_CAP`.

## Cost & Token Telemetry

The wrapper now ships per-call cost estimation (added 2026-04-25):

- **Output tokens**: counted exactly via `cl100k_base` (tiktoken). ±5% for non-OpenAI models.
- **Input tokens**: question tokens (exact) + 5,000-token RAG-context floor (estimate). Real retrieval depth is unobservable from outside the upstream; treat input as a lower bound.
- **Pricing table**: hard-coded in `PRICE_TABLE_USD_PER_M` for `gemini-2.5-{flash,flash-lite,pro}` and `gpt-{4o,4o-mini,5,5-mini}`. Unknown models fall through to a conservative `$1.00 in / $4.00 out` default and surface a ⚠️ warning.
- **First-call embedding cost**: NOT included in the per-call estimate (varies by repo size). Typical range: $0.01–$0.10. The footer flags it explicitly when `embeddings_cached=False`.
- **Per-call log**: every `ask_question` / `analyze_local_repo` invocation appends a JSON line to `~/.deepwiki-local-usage.jsonl` for grep-friendly weekly tally:
  ```bash
  jq -s 'map(.cost_total_usd) | add' ~/.deepwiki-local-usage.jsonl   # lifetime $
  jq -s '[.[] | select(.ts > "2026-04-19")] | map(.cost_total_usd) | add' ~/.deepwiki-local-usage.jsonl  # weekly
  ```

What the estimator does NOT do:

- **No pre-call dry-run preview**. (Future work — would require mirroring DeepWiki's retriever.)
- **No upstream usage harvesting**. The DeepWiki API streams plain model text and never emits a final SSE `usage` event, so the wrapper cannot extract authoritative numbers.
- **No provider billing reconciliation**. Numbers are estimates; verify against your provider dashboard for spend that matters.

## Configuration

**MCP registration** (in `~/.claude/.mcp.json` and `~/.claude.json`, identical entries):

```json
"deepwiki-local": {
  "command": "/home/riche/MCPs/deepwiki-open/.venv/bin/python",
  "args": ["/home/riche/MCPs/deepwiki-open/mcp_wrapper.py"],
  "env": { "DEEPWIKI_API_URL": "http://localhost:8001" }
}
```

**Wrapper defaults** (`mcp_wrapper.py`):

| Setting | Value |
|---|---|
| Provider / model | `google` / `gemini-2.5-flash` |
| Max response | 10 MB |
| Max stream duration | 600s |
| Chunk idle timeout | 30s |
| Connect timeout | 10s |
| Pre-stream retries | 2 (backoff 1s, 2s) |
| Path allowlist | `/home/riche/Proj` → `/host/projects`, `/home/riche/MCPs` → `/host/mcps` |

**Container resource limits** (upstream `docker-compose.yml`): 6 GB mem cap, 2 GB reservation, healthcheck every 60s with 30s start period.

## Volume Mounts

| Host | Container | Access | Why |
|---|---|---|---|
| `~/.adalflow` | `/root/.adalflow` | rw | RAG embeddings, repo clones, generated wikis. Persistence across container rebuilds. |
| `/home/riche/Proj` | `/host/projects` | **rw** | DeepWiki internally calls `os.makedirs` against the projects tree on local-repo analysis. Read-only fails. (Commit `3656377` flipped this from `:ro`.) |
| `/home/riche/MCPs` | `/host/mcps` | ro | Read-only is fine — DeepWiki does not mutate the MCP tree. |
| `./api/logs` | `/app/api/logs` | rw | Persistent app logs across restarts. |

## Operations

```bash
# Start
docker compose up -d

# Stop
docker compose down

# Restart after Dockerfile / api/ / src/ changes
docker compose up -d --build

# Logs
docker compose logs -f deepwiki

# Container health
curl -s http://localhost:8001/health | jq

# Standalone wrapper smoke test (does not need /mcp reconnect)
.venv/bin/python -c 'import asyncio, mcp_wrapper as m; \
  print(asyncio.run(m.health_check())); \
  print(asyncio.run(m.list_projects()))'
```

**Reconnect rule:** changes to `mcp_wrapper.py`, `docker-compose.override.yml`, or `.env` only require `/mcp` reconnect inside Claude Code. Changes to `api/`, `src/`, or `Dockerfile` require `docker compose up -d --build`.

## Security

- WSL2 NAT isolates the container from the Windows host network. Ports `8001` and `3000` bind to `0.0.0.0` inside the WSL2 interface but are not exposed to the LAN. Acceptable for single-user dev.
- `no-new-privileges` on the container.
- Path translation uses explicit allowlist; resolved paths are checked against allowed roots and rejected on traversal attempts.
- SSE/plain-text auto-detection on stream chunks (DeepWiki sends plain text under a `text/event-stream` Content-Type — wrapper does not trust the header).
- `.env` is `chmod 600` and `.gitignore`d.
- Auth mode disabled. Anyone with localhost access on this WSL2 instance can hit the API.

## Known Issues / Limits

| Issue | Impact | Mitigation |
|---|---|---|
| No pre-call cost preview | User can fire an expensive `ask_question` without preview | Per-call cost in metadata footer; weekly tally via jq |
| Input-token estimate is a floor | Cost may underestimate by 1.5–3× on large repos | Treat as floor; check `~/.deepwiki-local-usage.jsonl` |
| Default fallback price `$1/$4 per M` | Unknown models priced approximately | Add to `PRICE_TABLE_USD_PER_M` when adopting new models |
| `0.0.0.0` port binding | Visible to anything on the WSL2 interface | Acceptable in single-user dev; tighten with iptables if shared |
| Upstream pin drift | Upstream may have security/feature fixes not yet pulled | Run fork-sync periodically per `.fork-sync.yml` |
| No provider billing reconciliation | Estimates can drift from actual spend | Cross-check with provider dashboard for spend that matters |

## FYI / History

- **2026-04-25** — D1 (`health_check` provider listing fix, `list_projects` showing RAG caches), D2-A (tiktoken cost estimator + price table), D3 (per-call jsonl usage log + first-call embedding cost note in footer), CLAUDE.md rewrite.
- **2026-03-18** — Local directory analysis via `ask_question` (raw container path, not `local://`), SSE-vs-plaintext stream auto-detection, FastMCP 3.x deprecation fix, metadata footer added.
- **2026-03-15** — Initial integration, upstream pinned at `4c6a1f7`.

See `FYI.md` for full decision journal.

## Upstream Sync

- Effective merge point: `05591ee` (LaTeX PR #499). 2 upstream commits unmerged as of 2026-04-25:
  - `e8b6f1e` — Bitbucket clone auth fix (#509, low value for us)
  - `5b43df5` — "Deepwiki is coming back" (non-functional)
- Customizations live in seams that don't conflict with upstream: `mcp_wrapper.py` is a new file, `docker-compose.override.yml` is a new file, `.env`/`requirements-mcp.txt` are gitignored or non-conflicting. `.fork-sync.yml` lists `fork_owned_paths`: `CLAUDE.md`, `FYI.md`, `docs/PRD.md`, `mcp_wrapper.py`, `requirements-mcp.txt`, `docker-compose.override.yml`, `.env.sample`.
- Run fork-sync per `~/.claude/skills/fork-sync/`. Conflict surface should be minimal; if a sync touches our seams, treat as a red flag.

## Related

- `FYI.md` — append-only decision journal.
- `~/.deepwiki-local-usage.jsonl` — per-call cost log (created on first paid call).
- `~/.claude/.mcp.json` and `~/.claude.json` — MCP registration.
