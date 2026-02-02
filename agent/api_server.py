"""
FastAPI server for receiving prompts.

Exposes endpoints:
- POST /prompt: Queue a prompt for processing
- GET /health: Health check
- GET /status: Session status
"""

import asyncio
import logging
import os
import time
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

app = FastAPI(title="Claude Agent API", version="1.0.0")

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


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "service": "Claude Cloud Agent",
        "version": "1.0.0",
        "session_id": os.environ.get("SESSION_ID", "unknown")
    }
