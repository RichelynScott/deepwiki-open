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
from pathlib import Path

import httpx
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
    """Ask a question about any GitHub/GitLab/Bitbucket repository using RAG-powered analysis.

    This tool analyzes the repository codebase and uses retrieval-augmented generation
    to provide accurate, context-grounded answers. First call for a repo may take longer
    as it generates embeddings.

    Args:
        repo_url: Full repository URL (e.g., https://github.com/owner/repo)
        question: Question to ask about the repository
        provider: AI provider — google, openai, openrouter, ollama
        model: Model name (default: gemini-2.5-flash)
        language: Response language code (default: en)
        token: Personal access token for private repositories (optional)
    """
    payload = {
        "repo_url": repo_url,
        "type": _detect_repo_type(repo_url),
        "messages": [{"role": "user", "content": question}],
        "provider": provider,
        "model": model,
        "language": language,
    }
    if token:
        payload["token"] = token

    logger.info("ask_question: %s — %s", repo_url, question[:80])
    return await _stream_response("/chat/completions/stream", payload)


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
    """List all repositories that have been analyzed and cached locally.

    Returns a list of repositories that DeepWiki has previously processed,
    with their wiki generation status.
    """
    logger.info("list_projects")
    response = await _request_with_retry("GET", "/api/processed_projects")
    data = response.json()

    if not data:
        return "No projects have been analyzed yet. Use ask_question to analyze a repository."

    if isinstance(data, list):
        lines = [f"Cached projects ({len(data)}):\n"]
        for project in data:
            if isinstance(project, dict):
                name = project.get("name", project.get("repo", "unknown"))
                lines.append(f"  - {name}")
            else:
                lines.append(f"  - {project}")
        return "\n".join(lines)

    return json.dumps(data, indent=2)


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

    # If question provided, use the chat endpoint with local repo context
    payload = {
        "repo_url": f"local://{container_path}",
        "type": "local",
        "messages": [{"role": "user", "content": question}],
        "provider": "google",
        "model": "gemini-2.5-flash",
        "language": "en",
    }

    return await _stream_response("/chat/completions/stream", payload)


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

    # Check available models
    try:
        response = await _request_with_retry("GET", "/models/config")
        models = response.json()
        if isinstance(models, dict):
            providers = list(models.keys())
            results.append(f"Available providers: {', '.join(providers)}")
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
