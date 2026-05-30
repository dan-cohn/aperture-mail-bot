import httpx
from datetime import datetime, timedelta, timezone

_BASE_URL = "https://api.getmatter.com/public/v1"


class MatterClient:
    def __init__(self, api_key: str):
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def get_queue_items(self) -> list[dict]:
        """Return all queue items sorted by updated_at descending (most recent first)."""
        items: list[dict] = []
        cursor: str | None = None
        async with httpx.AsyncClient(timeout=15.0) as client:
            while True:
                params: dict = {"status": "queue", "order": "updated", "limit": 100}
                if cursor:
                    params["cursor"] = cursor
                resp = await client.get(
                    f"{_BASE_URL}/items",
                    headers=self._headers,
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()
                items.extend(data.get("results", []))
                if not data.get("has_more"):
                    break
                cursor = data.get("next_cursor")
        return items

    async def get_reading_seconds_last_7_days(self) -> int:
        """Sum seconds_read across all sessions in the past 7 days."""
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        total = 0
        cursor: str | None = None
        async with httpx.AsyncClient(timeout=15.0) as client:
            while True:
                params: dict = {"since": since, "limit": 100}
                if cursor:
                    params["cursor"] = cursor
                resp = await client.get(
                    f"{_BASE_URL}/reading_sessions",
                    headers=self._headers,
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()
                for session in data.get("results", []):
                    total += session.get("seconds_read", 0)
                if not data.get("has_more"):
                    break
                cursor = data.get("next_cursor")
        return total
