from __future__ import annotations

import logging
from uuid import uuid4

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, status

from ai_extractor import extract_with_ai, heuristic_extract_notification, parse_ai_notification
from models import CreateResponse, IntentRequest, NotificationRequest, NotificationType, RequestStatus, StatusResponse, StoredRequest
from provider_client import send_notification_with_retries
from store import _store_lock, requests_store

logger = logging.getLogger("intelligent-notification-service")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="Notification Service (Technical Test)")


@app.post("/v1/requests", status_code=status.HTTP_201_CREATED, response_model=CreateResponse)
async def create_request(payload: IntentRequest) -> CreateResponse:
    request_id = str(uuid4())
    async with _store_lock:
        requests_store[request_id] = StoredRequest(id=request_id, user_input=payload.user_input)
    return CreateResponse(id=request_id)


@app.post("/v1/requests/{request_id}/process", status_code=status.HTTP_202_ACCEPTED)
async def process_request(request_id: str, background_tasks: BackgroundTasks) -> dict[str, str]:
    async with _store_lock:
        item = requests_store.get(request_id)
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
        if item.status != RequestStatus.queued:
            return {"id": request_id, "status": item.status}
        item.status = RequestStatus.processing
    background_tasks.add_task(process_intent_request, request_id)
    return {"id": request_id, "status": RequestStatus.processing}


@app.get("/v1/requests/{request_id}", response_model=StatusResponse)
async def get_request_status(request_id: str) -> StatusResponse:
    item = requests_store.get(request_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    return StatusResponse(id=request_id, status=item.status)


async def process_intent_request(request_id: str) -> None:
    item = requests_store.get(request_id)
    if item is None:
        return

    ai_content = await extract_with_ai(item.user_input)
    notification = parse_ai_notification(ai_content) or heuristic_extract_notification(item.user_input)

    if notification is None:
        async with _store_lock:
            current = requests_store.get(request_id)
            if current:
                current.status = RequestStatus.failed
                current.error = "could not extract a valid notification"
        return

    delivered = await send_notification_with_retries(notification, request_id)
    async with _store_lock:
        current = requests_store.get(request_id)
        if current is None:
            return
        current.notification = notification
        current.status = RequestStatus.sent if delivered else RequestStatus.failed
        if not delivered and current.error is None:
            current.error = "provider delivery failed after retries"
