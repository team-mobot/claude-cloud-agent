"""
FastAPI server for receiving prompts.

Exposes endpoints:
- POST /prompt: Queue a prompt for processing
- GET /health: Health check
- GET /status: Session status
- /* : Proxy to dev server (for UAT preview)

Runs on port 3000 - ALB routes all traffic here. Non-API requests
are proxied to the dev server running on port 3001.
"""

import asyncio
import logging
import os
import time
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

app = FastAPI(title="Claude Agent API", version="1.0.0")

# Dev server URL for proxying (internal only)
DEV_SERVER_URL = "http://localhost:3001"

# HTTP client for proxying
_http_client: Optional[httpx.AsyncClient] = None


def get_http_client() -> httpx.AsyncClient:
    """Get or create async HTTP client for proxying."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=30.0)
    return _http_client

# Prompt queue (shared with main.py)
prompt_queue: asyncio.Queue = asyncio.Queue()

# Session state
session_start_time = time.time()
prompts_processed = 0


class PromptRequest(BaseModel):
    """Request body for /prompt endpoint."""
    prompt: str
    author: Optional[str] = "unknown"
    comment_id: Optional[int] = None


class PromptResponse(BaseModel):
    """Response body for /prompt endpoint."""
    message: str
    queue_position: int


class HealthResponse(BaseModel):
    """Response body for /health endpoint."""
    status: str
    session_id: str
    uptime_seconds: int


class StatusResponse(BaseModel):
    """Response body for /status endpoint."""
    session_id: str
    status: str
    uptime_seconds: int
    prompts_processed: int
    queue_size: int


@app.post("/prompt", response_model=PromptResponse)
async def submit_prompt(request: PromptRequest):
    """
    Submit a prompt to the processing queue.

    The prompt will be processed by Claude Code in order.
    """
    global prompts_processed

    logger.info(f"Received prompt from {request.author}: {request.prompt[:100]}...")

    # Add to queue
    await prompt_queue.put({
        "prompt": request.prompt,
        "author": request.author,
        "comment_id": request.comment_id,
        "submitted_at": time.time()
    })

    queue_size = prompt_queue.qsize()
    logger.info(f"Prompt queued, queue size: {queue_size}")

    return PromptResponse(
        message="Prompt queued for processing",
        queue_position=queue_size
    )


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """
    Health check endpoint.

    Used by load balancers to verify container health.
    """
    session_id = os.environ.get("SESSION_ID", "unknown")
    uptime = int(time.time() - session_start_time)

    return HealthResponse(
        status="healthy",
        session_id=session_id,
        uptime_seconds=uptime
    )


@app.get("/status", response_model=StatusResponse)
async def get_status():
    """
    Get detailed session status.
    """
    session_id = os.environ.get("SESSION_ID", "unknown")
    uptime = int(time.time() - session_start_time)

    return StatusResponse(
        session_id=session_id,
        status="running",
        uptime_seconds=uptime,
        prompts_processed=prompts_processed,
        queue_size=prompt_queue.qsize()
    )


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]
)
async def proxy_to_dev_server(request: Request, path: str):
    """
    Proxy all non-API requests to the dev server.

    This allows the ALB to route all traffic to port 3000 (this server),
    and we forward non-API requests to the dev server on port 3001.
    """
    # Don't proxy API paths (they should be handled by explicit routes above)
    # If we get here for an API path, it means the route wasn't found
    if path in ("prompt", "health", "status"):
        raise HTTPException(status_code=404, detail=f"/{path} should use explicit route")

    client = get_http_client()

    # Build target URL
    target_url = f"{DEV_SERVER_URL}/{path}"
    if request.query_params:
        target_url += f"?{request.query_params}"

    try:
        # Forward the request
        response = await client.request(
            method=request.method,
            url=target_url,
            headers={
                k: v for k, v in request.headers.items()
                if k.lower() not in ("host", "content-length")
            },
            content=await request.body() if request.method in ("POST", "PUT", "PATCH") else None,
        )

        # Return proxied response
        return StreamingResponse(
            content=response.iter_bytes(),
            status_code=response.status_code,
            headers={
                k: v for k, v in response.headers.items()
                if k.lower() not in ("content-encoding", "transfer-encoding", "content-length")
            },
            media_type=response.headers.get("content-type")
        )

    except httpx.ConnectError:
        # Dev server not running yet
        return {
            "error": "Dev server not available",
            "message": "The development server is starting up. Please wait a moment and refresh.",
            "session_id": os.environ.get("SESSION_ID", "unknown")
        }
    except Exception as e:
        logger.warning(f"Proxy error for {path}: {e}")
        raise HTTPException(status_code=502, detail=f"Proxy error: {e}")
