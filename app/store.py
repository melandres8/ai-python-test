from __future__ import annotations

import asyncio

from models import StoredRequest

requests_store: dict[str, StoredRequest] = {}
_store_lock = asyncio.Lock()
