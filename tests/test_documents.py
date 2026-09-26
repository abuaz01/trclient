from datetime import date

import httpx
import pytest

from trclient import documents
from trclient.errors import TRError

from fakes import PDF, make_client, server  # noqa: F401


@pytest.fixture
def presigned(monkeypatch):
    """Pre-signed URLs are fetched with a fresh client; record that no cookies are sent."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=PDF)

    monkeypatch.setattr(documents, "_storage_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True))
    return seen


async def test_list_documents_both_timelines(server, tmp_path):
    _, port = server
    async with make_client(port, tmp_path) as tr:
        docs = await tr.documents(since=date(2026, 1, 1))
    assert [d.id for d in docs] == ["doc-trade-1", "doc-statement-1"]
    trade, statement = docs
    assert trade.source == "transactions" and trade.event_title == "SAP" and trade.date == "2026-09-20"
    assert statement.source == "postbox" and statement.title == "Kontoauszug"
    assert "payload" not in statement.public()
    assert statement.filename() == "2026-09-01 - Kontoauszug - Kontoauszug - August 2026 [doc-stat].pdf"


async def test_filters(server, tmp_path):
    _, port = server
    async with make_client(port, tmp_path) as tr:
        assert [d.id for d in await tr.documents(since=date(2026, 1, 1), sources=("postbox",))] == ["doc-statement-1"]
        assert [d.id for d in await tr.documents(since=date(2026, 1, 1), title_contains="kontoauszug")] == [
            "doc-statement-1"]
        assert await tr.documents(since=date(2026, 9, 25)) == []


async def test_download(server, tmp_path, presigned):
    _, port = server
    out = tmp_path / "docs"
    async with make_client(port, tmp_path) as tr:
        docs = await tr.documents(since=date(2026, 1, 1))
        results = [await tr.download_document(d, out) for d in docs]
        assert [r["status"] for r in results] == ["downloaded", "downloaded"]
        assert all((out / d.filename()).read_bytes() == PDF for d in docs)
        assert (out / docs[0].filename()).stat().st_mode & 0o777 == 0o600
        assert (await tr.download_document(docs[0], out))["status"] == "exists"
    assert len(presigned) == 1 and "cookie" not in presigned[0].headers


async def test_refuses_plain_http(server, tmp_path):
    _, port = server
    doc = documents.Document(id="x", title="t", detail=None, date="2026-01-01", event_id="e", event_title="e",
                             event_subtitle=None, event_type=None, source="postbox", payload="http://evil/x.pdf")
    async with make_client(port, tmp_path) as tr:
        with pytest.raises(TRError, match="non-https"):
            await tr.download_document(doc, tmp_path)


def test_filename_is_sanitized():
    doc = documents.Document(id="abcdef123", title="A/B", detail=None, date="2026-01-01", event_id="e",
                             event_title="..\\x:y", event_subtitle=None, event_type=None, source="postbox",
                             payload="https://x")
    assert "/" not in doc.filename() and "\\" not in doc.filename()
