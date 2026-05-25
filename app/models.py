from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class NotificationType(str, Enum):
    email = "email"
    sms = "sms"


class RequestStatus(str, Enum):
    queued = "queued"
    processing = "processing"
    sent = "sent"
    failed = "failed"


class IntentRequest(BaseModel):
    user_input: str = Field(..., min_length=1)


class NotificationRequest(BaseModel):
    to: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    type: NotificationType


class CreateResponse(BaseModel):
    id: str


class StatusResponse(BaseModel):
    id: str
    status: RequestStatus


class StoredRequest(BaseModel):
    id: str
    user_input: str
    status: RequestStatus = RequestStatus.queued
    notification: NotificationRequest | None = None
    attempts: int = 0
    provider_id: str | None = None
    error: str | None = None
