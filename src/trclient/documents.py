"""Documents from the Trade Republic timeline: statements, trade confirmations, cost information,
tax documents and the postbox.

Trade Republic attaches documents to timeline events. Both timelines are read:
- timelineTransactions: trades, deposits, interest, dividends, card payments
- timelineActivityLog:  postbox items (account statements, tax reports, notices, contract documents)
Each event with details is opened with timelineDetailV2; its "documents" section lists the files.
A document's action payload is either a pre-signed https URL (downloaded without cookies) or
{"path": ...} relative to api.traderepublic.com (downloaded with the session cookies).
"""

from __future__ import annotations

import os
import pathlib
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from .errors import TRError

if TYPE_CHECKING:
    from .client import TradeRepublic

TIMELINES = ("timelineTransactions", "timelineActivityLog")
MAX_PAGES = 500


@dataclass(frozen=True)
class Document:
    id: str
    title: str                 # e.g. "Kontoauszug", "Abrechnung", "Kosteninformation"
    detail: str | None         # e.g. the date shown under the title in the app
    date: str                  # YYYY-MM-DD of the timeline event
    event_id: str
    event_title: str           # e.g. instrument name or "Kontoauszug"
    event_subtitle: str | None
    event_type: str | None
    source: str                # transactions | postbox
    payload: Any               # URL string or {"path": ...}

    def public(self) -> dict:
        """Everything except the download link (pre-signed URLs are credentials)."""
        d = asdict(self)
        d.pop("payload")
        return d

    def filename(self) -> str:
        parts = [self.date, self.title, self.event_title]
        if self.event_subtitle:
            parts.append(self.event_subtitle)
        name = " - ".join(p for p in parts if p)
        name = re.sub(r"[\x00-\x1f/\\:*?\"<>|]+", "_", name).strip(" .")[:150]
        return f"{name} [{self.id[:8]}].pdf"


def _event_date(event: dict) -> date | None:
    ts = str(event.get("timestamp") or "")[:10]
    try:
        return date.fromisoformat(ts)
    except ValueError:
        return None


async def _events(tr: "TradeRepublic", timeline: str, since: date | None) -> list[dict]:
    events: list[dict] = []
    after = None
    for _ in range(MAX_PAGES):
        page = await tr._query({"type": timeline, "after": after})
        items = page.get("items") or [] if isinstance(page, dict) else []
        reached_since = False
        for event in items:
            day = _event_date(event)
            if since and day and day < since:
                reached_since = True
                break
            events.append(event)
        after = (page.get("cursors") or {}).get("after") if isinstance(page, dict) else None
        if reached_since or not after:
            break
    return events


def _has_details(event: dict) -> bool:
    action = event.get("action") or {}
    return action.get("type") == "timelineDetail" and action.get("payload") == event.get("id")


async def list_documents(
    tr: "TradeRepublic",
    *,
    since: date | None = None,
    sources: tuple[str, ...] = ("transactions", "postbox"),
    title_contains: str | None = None,
) -> list[Document]:
    """All documents of events on or after `since` (newest first)."""
    docs: list[Document] = []
    seen: set[str] = set()
    for timeline, source in zip(TIMELINES, ("transactions", "postbox")):
        if source not in sources:
            continue
        for event in await _events(tr, timeline, since):
            if event.get("id") in seen or not _has_details(event):
                continue
            seen.add(event["id"])
            detail = await tr.timeline_detail(event["id"])
            day = _event_date(event)
            for section in (detail or {}).get("sections") or []:
                if section.get("type") != "documents":
                    continue
                for item in section.get("data") or []:
                    payload = (item.get("action") or {}).get("payload")
                    if not payload or not item.get("id"):
                        continue
                    doc = Document(
                        id=str(item["id"]),
                        title=str(item.get("title") or "Dokument"),
                        detail=item.get("detail"),
                        date=day.isoformat() if day else "",
                        event_id=str(event["id"]),
                        event_title=str(event.get("title") or ""),
                        event_subtitle=event.get("subtitle"),
                        event_type=event.get("eventType"),
                        source=source,
                        payload=payload,
                    )
                    if title_contains and title_contains.lower() not in f"{doc.title} {doc.event_title}".lower():
                        continue
                    docs.append(doc)
    docs.sort(key=lambda d: d.date, reverse=True)
    return docs


def _storage_client() -> httpx.AsyncClient:
    """Separate client without cookies for the pre-signed storage URLs."""
    return httpx.AsyncClient(timeout=60, follow_redirects=True)


def _check_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise TRError("refusing to download a document from a non-https URL")
    return url


async def download(tr: "TradeRepublic", doc: Document, directory: pathlib.Path, *, overwrite: bool = False) -> dict:
    """Save one document as PDF in `directory`. Existing files are kept unless overwrite=True."""
    directory = directory.expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / doc.filename()
    if target.exists() and not overwrite:
        return {"id": doc.id, "file": str(target), "status": "exists"}
    if isinstance(doc.payload, dict) and isinstance(doc.payload.get("path"), str):
        path = "/" + doc.payload["path"].lstrip("/")
        content = (await tr.session.request("GET", path)).content
    elif isinstance(doc.payload, str):
        # pre-signed storage URL: plain GET, the session cookies are never sent to it
        async with _storage_client() as http:
            r = await http.get(_check_url(doc.payload))
            if r.status_code >= 400:
                raise TRError(f"document download failed with HTTP {r.status_code}")
            content = r.content
    else:
        raise TRError(f"unsupported document link for {doc.id}")
    tmp = target.with_suffix(".part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(content)
    tmp.replace(target)
    return {"id": doc.id, "file": str(target), "status": "downloaded", "bytes": len(content)}


def parse_since(value: str | None, default_days: int | None = None) -> date | None:
    if value:
        return date.fromisoformat(value)
    if default_days is not None:
        return date.fromordinal(datetime.now().date().toordinal() - default_days)
    return None
