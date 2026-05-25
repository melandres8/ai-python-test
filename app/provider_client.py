from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx
from tenacity import retry, retry_if_result, stop_after_attempt, wait_exponential_jitter

from models import NotificationRequest
from store import requests_store

logger = logging.getLogger("intelligent-notification-service")

PROVIDER_BASE_URL = os.getenv("PROVIDER_BASE_URL", "http://localhost:3001")
PROVIDER_API_KEY = os.getenv("PROVIDER_API_KEY", "test-dev-2026")
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "6.0"))
MAX_NOTIFY_CONCURRENCY = int(os.getenv("MAX_NOTIFY_CONCURRENCY", "25"))

_notify_semaphore = asyncio.Semaphore(MAX_NOTIFY_CONCURRENCY)


@retry(
    retry=retry_if_result(lambda result: result is False),
    wait=wait_exponential_jitter(initial=0.1, max=1.0),
    stop=stop_after_attempt(4),
    retry_error_callback=lambda retry_state: False,
    reraise=False,
)
async def send_notification_with_retries(notification: NotificationRequest, request_id: str) -> bool:
    async with _notify_semaphore:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            try:
                response = await client.post(
                    f"{PROVIDER_BASE_URL}/v1/notify",
                    headers={"X-API-Key": PROVIDER_API_KEY, "Content-Type": "application/json"},
                    params={"trace_id": request_id, "priority": "normal"},
                    json=notification.model_dump(),
                )
            except httpx.HTTPError as exc:
                logger.warning("Notification provider failed for %s: %s", request_id, exc)
                return False

    if response.status_code == 200:
        try:
            body: dict[str, Any] = response.json()
        except ValueError:
            body = {}
        requests_store[request_id].provider_id = body.get("provider_id")
        return True

    if response.status_code in {429, 500, 502, 503, 504}:
        logger.info("Retryable notification status for %s: %s", request_id, response.status_code)
        return False

    requests_store[request_id].error = f"provider returned {response.status_code}: {response.text[:200]}"
    return False
