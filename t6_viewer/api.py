"""Small client for Tesla's authenticated dashcam key endpoint."""

from __future__ import annotations

import base64
from typing import Callable, Iterable

import requests

API_URL = "https://dashcam.tesla.com/api/1/decrypt/batch"


def fetch_keys(
    token: str,
    headers: Iterable[dict],
    batch_size: int = 20,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, bytes]:
    """Fetch per-file AES keys; encrypted video bytes never leave the PC."""
    token = token.strip()
    if not token:
        raise ValueError("Tesla Bearer token is empty")
    all_headers = list(headers)
    result: dict[str, bytes] = {}
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Origin": "https://dashcam.tesla.com",
            "Referer": "https://dashcam.tesla.com/",
        }
    )
    total_batches = (len(all_headers) + batch_size - 1) // batch_size
    for batch_index, start in enumerate(range(0, len(all_headers), batch_size), 1):
        batch = all_headers[start : start + batch_size]
        payload = {"items": [_api_item(item) for item in batch]}
        response = session.post(API_URL, json=payload, timeout=45)
        if response.status_code == 401:
            raise PermissionError("Tesla token is expired or invalid")
        response.raise_for_status()
        for item in response.json().get("results", []):
            if item.get("error"):
                continue
            try:
                result[item["id"]] = base64.b64decode(item["key"], validate=True)
            except (KeyError, ValueError) as exc:
                raise RuntimeError(f"Tesla returned an invalid key response: {exc}") from exc
        if progress:
            progress(batch_index, total_batches)
    return result


def _api_item(header: dict) -> dict:
    fields = ("id", "vin", "key_id", "timestamp", "wrapped_key", "public_key")
    if all(field in header for field in fields):
        return {field: header[field] for field in fields}
    return {"id": header["id"]}
