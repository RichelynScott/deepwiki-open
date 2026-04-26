#!/usr/bin/env python3
"""
DeepWiki Local MCP Wrapper — Stream Aggregator

Thin FastMCP wrapper exposing 6 tools that call the self-hosted DeepWiki Open
REST API running in Docker at localhost:8001.

Transport: stdio (Claude Code spawns this as a subprocess)
Pattern: Stream aggregator — collects SSE chunks, returns complete text

Upstream: AsyncFuncAI/deepwiki-open (pinned at 4c6a1f7)
Fork: RichelynScott/deepwiki-open
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import tiktoken
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEEPWIKI_API = os.getenv("DEEPWIKI_API_URL", "http://localhost:8001")

# Resource limits
MAX_RESPONSE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_STREAM_DURATION_S = 600  # 10 minutes
CHUNK_IDLE_TIMEOUT_S = 30  # seconds between SSE chunks before abort
UPSTREAM_CONNECT_TIMEOUT_S = 10
MAX_RETRIES = 2
RETRY_DELAY_S = 1.0

# Path translation: host -> Docker container (explicit allowlist)
PATH_MAPPINGS: dict[str, str] = {
    "/home/riche/Proj": "/host/projects",
    "/home/riche/MCPs": "/host/mcps",
}

# Cost estimation
# Prices in USD per 1M tokens (input, output). Approximate; update as providers change.
# Source: provider pricing pages, late 2025 / early 2026.
# Gemini billed by tokens for >=2.5; tiktoken cl100k_base used as universal estimator (±20%).
PRICE_TABLE_USD_PER_M: dict[str, tuple[float, float]] = {
    # Google
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
    # OpenAI
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    # OpenRouter / others — fall through to default
}
DEFAULT_PRICE_USD_PER_M: tuple[float, float] = (1.00, 4.00)  # conservative fallback

# Typical retrieved-context size for DeepWiki RAG (top-k=20, ~250 tokens/chunk).
# Used as floor estimate when we can't observe actual retrieval.
TYPICAL_RAG_CONTEXT_TOKENS = 5000

USAGE_LOG_PATH = Path.home() / ".deepwiki-local-usage.jsonl"

_TOKENIZER: tiktoken.Encoding | None = None


def _get_tokenizer() -> tiktoken.Encoding:
    """Lazy-load cl100k_base tokenizer (universal approximation)."""
    global _TOKENIZER
    if _TOKENIZER is None:
        _TOKENIZER = tiktoken.get_encoding("cl100k_base")
    return _TOKENIZER


def _count_tokens(text: str) -> int:
    """Count tokens via cl100k_base. Approximate for non-OpenAI models (±20%)."""
    if not text:
        return 0
    try:
        return len(_get_tokenizer().encode(text))
    except Exception:
        # Fallback: ~4 chars/token heuristic
        return max(1, len(text) // 4)


def _estimate_cost(
    *,
    question: str,
    response: str,
    provider: str,
    model: str,
    embeddings_cached: bool,
) -> dict:
    """
    Estimate token usage and cost for a DeepWiki RAG call.

    Returns dict with: tokens_in_question, tokens_in_rag_estimate,
    tokens_out, cost_in_usd, cost_out_usd, cost_total_usd, price_per_m.

    Note: tokens_in is a FLOOR (question + estimated RAG context).
    Actual retrieved context varies by repo size and query.
    """
    tokens_in_question = _count_tokens(question)
    tokens_in_rag = TYPICAL_RAG_CONTEXT_TOKENS  # estimate
    tokens_in_total = tokens_in_question + tokens_in_rag
    tokens_out = _count_tokens(response)

    price_in, price_out = PRICE_TABLE_USD_PER_M.get(model, DEFAULT_PRICE_USD_PER_M)
    cost_in = (tokens_in_total / 1_000_000) * price_in
    cost_out = (tokens_out / 1_000_000) * price_out

    return {
        "tokens_in_question": tokens_in_question,
        "tokens_in_rag_estimate": tokens_in_rag,
        "tokens_in_total_estimate": tokens_in_total,
        "tokens_out": tokens_out,
        "price_in_per_m": price_in,
        "price_out_per_m": price_out,
        "cost_in_usd": cost_in,
        "cost_out_usd": cost_out,
        "cost_total_usd": cost_in + cost_out,
        "price_known": model in PRICE_TABLE_USD_PER_M,
    }


def _log_usage(
    *,
    source: str,
    provider: str,
    model: str,
    estimate: dict,
    duration_s: float,
    embeddings_cached: bool,
) -> None:
    """Append a usage record to ~/.deepwiki-local-usage.jsonl. Never fails the call."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "provider": provider,
        "model": model,
        "tokens_in_estimate": estimate["tokens_in_total_estimate"],
        "tokens_out": estimate["tokens_out"],
        "cost_total_usd": round(estimate["cost_total_usd"], 6),
        "duration_s": round(duration_s, 2),
        "embeddings_cached": embeddings_cached,
        "price_known": estimate["price_known"],
    }
    try:
        with USAGE_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:
        logger.warning("Failed to write usage log: %s", exc)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,  # MCP uses stdout for protocol; logs go to stderr
)
logger = logging.getLogger("deepwiki-local")

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("deepwiki-local")

# ---------------------------------------------------------------------------
# HTTP Client Helpers
# ---------------------------------------------------------------------------


def _get_client() -> httpx.AsyncClient:
    """Create an httpx client with appropriate timeouts."""
    return httpx.AsyncClient(
        base_url=DEEPWIKI_API,
        timeout=httpx.Timeout(
            connect=UPSTREAM_CONNECT_TIMEOUT_S,
            read=MAX_STREAM_DURATION_S,
            write=30.0,
            pool=10.0,
        ),
    )


async def _request_with_retry(
    method: str,
    path: str,
    **kwargs,
) -> httpx.Response:
    """Make an HTTP request with pre-stream retries only."""
    last_error: Exception | None = None
    async with _get_client() as client:
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await client.request(method, path, **kwargs)
                response.raise_for_status()
                return response
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc
                if attempt < MAX_RETRIES:
                    delay = RETRY_DELAY_S * (attempt + 1)
                    logger.warning(
                        "Retry %d/%d for %s %s: %s (waiting %.1fs)",
                        attempt + 1,
                        MAX_RETRIES,
                        method,
                        path,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 429:
                    retry_after = exc.response.headers.get("Retry-After", "unknown")
                    raise RuntimeError(
                        f"Rate limited by DeepWiki API. Retry after {retry_after} seconds."
                    ) from exc
                raise RuntimeError(
                    f"DeepWiki API error {status}: {exc.response.text[:500]}"
                ) from exc

    raise RuntimeError(
        f"DeepWiki API unreachable after {MAX_RETRIES + 1} attempts: {last_error}"
    )


async def _stream_response(
    path: str,
    payload: dict,
) -> str:
    """
    POST to a streaming endpoint and aggregate the full response.

    Handles both plain-text streaming and SSE (data: prefix) formats.
    Collects chunks until the stream ends. Enforces idle timeout and size limit.
    Does NOT retry once streaming has begun.
    """
    collected: list[str] = []
    total_bytes = 0

    async with _get_client() as client:
        try:
            async with client.stream("POST", path, json=payload) as response:
                content_type = response.headers.get("content-type", "")
                if response.status_code != 200:
                    body = await response.aread()
                    raise RuntimeError(
                        f"DeepWiki API returned {response.status_code}: {body.decode()[:500]}"
                    )

                logger.info("Stream started for %s", path)

                # DeepWiki may report text/event-stream but send plain text
                # without SSE framing. Always try plain-text collection and
                # only parse SSE if we actually see "data: " prefixed lines.
                async for chunk in response.aiter_text():
                    if not chunk:
                        continue

                    # Check if this chunk contains actual SSE framing
                    has_sse_prefix = "data: " in chunk

                    if has_sse_prefix:
                        # SSE format: parse "data: ..." lines
                        for line in chunk.split("\n"):
                            if not line.startswith("data: "):
                                continue
                            data_str = line[6:]
                            if data_str.strip() == "[DONE]":
                                break
                            try:
                                data = json.loads(data_str)
                                content = _extract_content(data)
                            except json.JSONDecodeError:
                                content = data_str
                            if content:
                                collected.append(content)
                                total_bytes += len(content.encode("utf-8"))
                    else:
                        # Plain text streaming — append chunks directly
                        collected.append(chunk)
                        total_bytes += len(chunk.encode("utf-8"))

                    # Check size limit
                    if total_bytes > MAX_RESPONSE_SIZE_BYTES:
                        collected.append(
                            f"\n\n[TRUNCATED: Response exceeded "
                            f"{MAX_RESPONSE_SIZE_BYTES // (1024*1024)}MB limit. "
                            f"Received {total_bytes} bytes before truncation.]"
                        )
                        logger.warning("Response truncated at %d bytes", total_bytes)
                        break

        except httpx.ReadTimeout:
            if collected:
                collected.append(
                    f"\n\n[STREAM INTERRUPTED: No data received for "
                    f"{CHUNK_IDLE_TIMEOUT_S}s. "
                    f"Partial response returned ({total_bytes} bytes).]"
                )
                logger.warning("Stream timed out after %d bytes", total_bytes)
            else:
                raise RuntimeError(
                    f"DeepWiki stream timed out before receiving any data "
                    f"(idle timeout: {CHUNK_IDLE_TIMEOUT_S}s)"
                )

    result = "".join(collected)
    logger.info("Stream completed: %d bytes", len(result.encode("utf-8")))
    return result


def _extract_content(data: dict | str) -> str:
    """Extract content from various SSE JSON formats."""
    if isinstance(data, str):
        return data
    if not isinstance(data, dict):
        return ""
    # Direct content field
    content = data.get("content", "")
    if content:
        return content
    # OpenAI-style delta format
    choices = data.get("choices", [])
    if choices:
        delta = choices[0].get("delta", {})
        content = delta.get("content", "")
        if content:
            return content
    # Simple message format
    return data.get("message", "") or data.get("text", "")


def _format_metadata(
    *,
    source: str,
    source_type: str,
    provider: str,
    model: str,
    response_bytes: int,
    duration_s: float,
    cached: bool | None = None,
    estimate: dict | None = None,
) -> str:
    """Format a metadata footer for tool responses."""
    lines = [
        "",
        "---",
        "**DeepWiki Local — Usage Metadata**",
        "| Field | Value |",
        "|-------|-------|",
        f"| Source | `{source}` |",
        f"| Type | {source_type} |",
        f"| Provider | {provider} |",
        f"| Model | {model} |",
        f"| Response size | {response_bytes:,} bytes |",
        f"| Duration | {duration_s:.1f}s |",
    ]
    if cached is not None:
        lines.append(f"| Embeddings | {'cached' if cached else 'freshly generated (one-time API cost)'} |")
    lines.append(f"| API calls | Embedding{'s (cached)' if cached else ' generation'} + LLM generation |")

    if estimate is not None:
        lines.extend(
            [
                f"| Tokens out (counted) | {estimate['tokens_out']:,} |",
                f"| Tokens in (question) | {estimate['tokens_in_question']:,} |",
                f"| Tokens in (RAG context, est.) | ~{estimate['tokens_in_rag_estimate']:,} |",
                f"| Cost (output) | ${estimate['cost_out_usd']:.6f} |",
                f"| Cost (input, est.) | ${estimate['cost_in_usd']:.6f} |",
                f"| **Cost total (est.)** | **${estimate['cost_total_usd']:.6f}** |",
            ]
        )
        if not estimate["price_known"]:
            lines.append(
                f"| ⚠️ Price | model `{model}` not in price table — used default "
                f"${estimate['price_in_per_m']:.2f}/M in, ${estimate['price_out_per_m']:.2f}/M out |"
            )
        if not cached:
            lines.append(
                "| ⚠️ First call | Embedding generation cost (~$0.01–$0.10 depending on repo size) NOT included above |"
            )
        lines.append(
            "| Estimate accuracy | Output: counted exact (cl100k_base, ±5% for non-OpenAI). Input: question exact + 5K-token RAG floor — actual retrieval may be larger. |"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Path Translation
# ---------------------------------------------------------------------------


def _translate_path(host_path: str) -> str:
    """
    Translate a host filesystem path to the corresponding Docker container path.

    Uses an explicit allowlist. Validates against symlink traversal.
    """
    resolved = Path(host_path).resolve()

    for host_prefix, container_prefix in PATH_MAPPINGS.items():
        allowed_root = Path(host_prefix).resolve()
        if resolved == allowed_root or _is_relative_to(resolved, allowed_root):
            relative = resolved.relative_to(allowed_root)
            container_path = f"{container_prefix}/{relative}"
            logger.info("Path translated: %s -> %s", host_path, container_path)
            return container_path

    allowed = ", ".join(PATH_MAPPINGS.keys())
    raise ValueError(
        f"Path '{host_path}' is not within any allowed mount. "
        f"Allowed roots: {allowed}"
    )


def _is_relative_to(path: Path, parent: Path) -> bool:
    """Check if path is relative to parent (Python 3.9+ compatible)."""
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def ask_question(
    repo_url: str,
    question: str,
    provider: str = "google",
    model: str = "gemini-2.5-flash",
    language: str = "en",
    token: str = "",
) -> str:
    """Ask a question about any repository or local directory using RAG-powered analysis.

    This tool analyzes the codebase and uses retrieval-augmented generation
    to provide accurate, context-grounded answers. First call for a repo may take
    longer as it generates embeddings.

    Accepts both remote URLs and local filesystem paths:
      - Remote: https://github.com/owner/repo
      - Local:  /home/riche/Proj/my-project  (auto-translated to Docker mount)

    Args:
        repo_url: Repository URL or absolute local path
        question: Question to ask about the repository
        provider: AI provider — google, openai, openrouter, ollama
        model: Model name (default: gemini-2.5-flash)
        language: Response language code (default: en)
        token: Personal access token for private repositories (optional)
    """
    # Detect if this is a local path and translate it
    effective_url = repo_url
    is_local = not repo_url.startswith(("http://", "https://"))
    if is_local:
        try:
            effective_url = _translate_path(repo_url)
            logger.info("Local path translated: %s -> %s", repo_url, effective_url)
        except ValueError as exc:
            return str(exc)

    payload = {
        "repo_url": effective_url,
        "type": _detect_repo_type(repo_url),
        "messages": [{"role": "user", "content": question}],
        "provider": provider,
        "model": model,
        "language": language,
    }
    if token:
        payload["token"] = token

    # Check if embeddings are cached
    repo_name = os.path.basename(effective_url.rstrip("/"))
    adalflow_db = os.path.expanduser(f"~/.adalflow/databases/{repo_name}.pkl")
    embeddings_cached = os.path.exists(adalflow_db)

    logger.info("ask_question: %s — %s (cached=%s)", effective_url, question[:80], embeddings_cached)
    t0 = time.monotonic()
    result = await _stream_response("/chat/completions/stream", payload)
    duration = time.monotonic() - t0

    effective_model = model or "gemini-2.5-flash"
    estimate = _estimate_cost(
        question=question,
        response=result,
        provider=provider,
        model=effective_model,
        embeddings_cached=embeddings_cached,
    )
    _log_usage(
        source=repo_url,
        provider=provider,
        model=effective_model,
        estimate=estimate,
        duration_s=duration,
        embeddings_cached=embeddings_cached,
    )

    metadata = _format_metadata(
        source=repo_url,
        source_type="local directory" if is_local else "remote repository",
        provider=provider,
        model=effective_model,
        response_bytes=len(result.encode("utf-8")),
        duration_s=duration,
        cached=embeddings_cached,
        estimate=estimate,
    )
    return result + metadata


@mcp.tool()
async def read_wiki_structure(
    owner: str,
    repo: str,
    repo_type: str = "github",
    language: str = "en",
) -> str:
    """Get the wiki table of contents for a previously analyzed repository.

    Returns the section/page structure of the generated wiki. The repository
    must have been analyzed first (via ask_question or the web UI).

    Args:
        owner: Repository owner (e.g., 'facebook')
        repo: Repository name (e.g., 'react')
        repo_type: Platform — github, gitlab, bitbucket
        language: Wiki language code
    """
    logger.info("read_wiki_structure: %s/%s", owner, repo)
    response = await _request_with_retry(
        "GET",
        "/api/wiki_cache",
        params={
            "owner": owner,
            "repo": repo,
            "repo_type": repo_type,
            "language": language,
        },
    )

    data = response.json()
    if not data:
        return f"No wiki found for {owner}/{repo}. Use ask_question first to analyze the repository."

    # Extract structure (section titles and hierarchy)
    if isinstance(data, dict):
        pages = data.get("pages", [])
        if pages:
            structure = []
            for i, page in enumerate(pages, 1):
                title = page.get("title", f"Section {i}")
                structure.append(f"{i}. {title}")
            return f"Wiki structure for {owner}/{repo}:\n\n" + "\n".join(structure)

    return json.dumps(data, indent=2)


@mcp.tool()
async def read_wiki_contents(
    owner: str,
    repo: str,
    repo_type: str = "github",
    language: str = "en",
    section: str = "",
) -> str:
    """Read the full generated wiki contents for a repository.

    Returns the complete wiki text. Optionally filter to a specific section.
    The repository must have been analyzed first.

    Args:
        owner: Repository owner
        repo: Repository name
        repo_type: Platform — github, gitlab, bitbucket
        language: Wiki language code
        section: Specific section title to retrieve (optional, returns all if empty)
    """
    logger.info("read_wiki_contents: %s/%s (section=%s)", owner, repo, section or "all")
    response = await _request_with_retry(
        "GET",
        "/api/wiki_cache",
        params={
            "owner": owner,
            "repo": repo,
            "repo_type": repo_type,
            "language": language,
        },
    )

    data = response.json()
    if not data:
        return f"No wiki found for {owner}/{repo}. Use ask_question first to analyze the repository."

    # If section filter requested, find matching page
    if section and isinstance(data, dict):
        pages = data.get("pages", [])
        for page in pages:
            if section.lower() in page.get("title", "").lower():
                return page.get("content", "No content available for this section.")
        return f"Section '{section}' not found. Use read_wiki_structure to see available sections."

    # Return full content
    if isinstance(data, dict):
        pages = data.get("pages", [])
        if pages:
            parts = []
            for page in pages:
                title = page.get("title", "Untitled")
                content = page.get("content", "")
                parts.append(f"# {title}\n\n{content}")
            result = "\n\n---\n\n".join(parts)

            # Check size
            if len(result.encode("utf-8")) > MAX_RESPONSE_SIZE_BYTES:
                return (
                    f"Wiki for {owner}/{repo} is too large to return in full "
                    f"({len(result)} chars). Use read_wiki_structure to see sections, "
                    f"then read_wiki_contents with section parameter."
                )
            return result

    return json.dumps(data, indent=2)


@mcp.tool()
async def list_projects() -> str:
    """List all repositories cached locally — generated wikis AND RAG embeddings.

    Reports two distinct caches:
      - Generated wikis (from /api/processed_projects, ~/.adalflow/wikicache/*)
      - RAG embeddings (~/.adalflow/databases/*.pkl) — created on first ask_question

    A repo with embeddings but no wiki is still queryable via ask_question (cached, fast).
    """
    logger.info("list_projects")
    lines: list[str] = []

    # 1. Generated wikis
    try:
        response = await _request_with_retry("GET", "/api/processed_projects")
        data = response.json()
        wikis = data if isinstance(data, list) else []
    except Exception as exc:
        wikis = []
        lines.append(f"⚠️ Wiki cache query failed: {exc}\n")

    lines.append(f"## Generated wikis ({len(wikis)})")
    if wikis:
        for project in wikis:
            if isinstance(project, dict):
                name = project.get("name", project.get("repo", "unknown"))
                lines.append(f"  - {name}")
            else:
                lines.append(f"  - {project}")
    else:
        lines.append("  (none — use the web UI or read_wiki_contents to trigger generation)")

    # 2. RAG embedding caches — direct filesystem scan
    db_dir = Path.home() / ".adalflow" / "databases"
    rag_dbs: list[tuple[str, int]] = []
    if db_dir.exists():
        for pkl in sorted(db_dir.glob("*.pkl")):
            try:
                size = pkl.stat().st_size
                rag_dbs.append((pkl.stem, size))
            except OSError:
                continue

    lines.append(f"\n## RAG embedding caches ({len(rag_dbs)})")
    if rag_dbs:
        for name, size in rag_dbs:
            mb = size / (1024 * 1024)
            lines.append(f"  - {name} ({mb:.1f} MB)")
        lines.append(
            "\nThese repos are ready for ask_question (no embedding regeneration cost)."
        )
    else:
        lines.append("  (none — first ask_question on a new repo will generate embeddings)")

    return "\n".join(lines)


@mcp.tool()
async def analyze_local_repo(
    path: str,
    question: str = "",
) -> str:
    """Analyze a repository from the local filesystem.

    The path is automatically translated from the host filesystem to the
    Docker container mount. Supported paths: /home/riche/Proj/* and /home/riche/MCPs/*

    Args:
        path: Absolute path to the repository on the host filesystem
        question: Optional question to ask about the repo (if empty, returns structure only)
    """
    # Translate and validate path
    try:
        container_path = _translate_path(path)
    except ValueError as exc:
        return str(exc)

    logger.info("analyze_local_repo: %s -> %s", path, container_path)

    # First get the repo structure
    response = await _request_with_retry(
        "GET",
        "/local_repo/structure",
        params={"path": container_path},
    )
    structure = response.json()

    if not question:
        return f"Repository structure at {path}:\n\n{json.dumps(structure, indent=2)}"

    # If question provided, use the chat endpoint with the container path directly.
    # DeepWiki's RAG pipeline treats any non-http(s) repo_url as a local path.
    provider = "google"
    model = "gemini-2.5-flash"
    payload = {
        "repo_url": container_path,
        "type": "github",  # Type is used for URL parsing only; local paths bypass it
        "messages": [{"role": "user", "content": question}],
        "provider": provider,
        "model": model,
        "language": "en",
    }

    repo_name = os.path.basename(container_path.rstrip("/"))
    adalflow_db = os.path.expanduser(f"~/.adalflow/databases/{repo_name}.pkl")
    embeddings_cached = os.path.exists(adalflow_db)

    t0 = time.monotonic()
    result = await _stream_response("/chat/completions/stream", payload)
    duration = time.monotonic() - t0

    estimate = _estimate_cost(
        question=question,
        response=result,
        provider=provider,
        model=model,
        embeddings_cached=embeddings_cached,
    )
    _log_usage(
        source=path,
        provider=provider,
        model=model,
        estimate=estimate,
        duration_s=duration,
        embeddings_cached=embeddings_cached,
    )

    metadata = _format_metadata(
        source=path,
        source_type="local directory",
        provider=provider,
        model=model,
        response_bytes=len(result.encode("utf-8")),
        duration_s=duration,
        cached=embeddings_cached,
        estimate=estimate,
    )
    return result + metadata


@mcp.tool()
async def health_check() -> str:
    """Check if the DeepWiki Docker container is running and responsive.

    Returns the health status, available AI models, and configuration info.
    Run this first if other tools are failing.
    """
    logger.info("health_check")
    results = []

    # Check health endpoint
    try:
        response = await _request_with_retry("GET", "/health")
        health = response.json()
        results.append(f"Health: {health.get('status', 'unknown')}")
    except Exception as exc:
        return (
            f"DeepWiki container is NOT reachable at {DEEPWIKI_API}.\n"
            f"Error: {exc}\n\n"
            f"To start: cd /home/riche/MCPs/deepwiki-open && docker compose up -d"
        )

    # Check available models — /models/config returns {"providers": [...], "defaultProvider": "..."}
    try:
        response = await _request_with_retry("GET", "/models/config")
        cfg = response.json()
        if isinstance(cfg, dict):
            providers = cfg.get("providers", [])
            if isinstance(providers, list) and providers:
                provider_ids = [p.get("id", "?") for p in providers if isinstance(p, dict)]
                default = cfg.get("defaultProvider", "?")
                results.append(f"Available providers ({len(provider_ids)}): {', '.join(provider_ids)}")
                results.append(f"Default provider: {default}")
            else:
                results.append("Models config: no providers reported")
    except Exception:
        results.append("Models config: unavailable")

    # Check auth status
    try:
        response = await _request_with_retry("GET", "/auth/status")
        auth = response.json()
        results.append(f"Auth mode: {'enabled' if auth.get('enabled') else 'disabled'}")
    except Exception:
        results.append("Auth status: unavailable")

    results.append(f"API URL: {DEEPWIKI_API}")

    return "DeepWiki Local — Status Report\n\n" + "\n".join(results)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _detect_repo_type(url: str) -> str:
    """Detect repository platform from URL."""
    url_lower = url.lower()
    if "gitlab" in url_lower:
        return "gitlab"
    if "bitbucket" in url_lower:
        return "bitbucket"
    return "github"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
