"""First query for faculty and researchers: recent papers in their field."""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.models import dig
from app.playbooks.base import PillDef, Rung


def ladder(field_name: str) -> list[Rung]:
    query = field_name or "machine learning"
    rungs = [Rung(f"Papers on {query}", {"query": query, "sources": ["scholar-articles"]})]
    shorter = " ".join(query.split()[:2])
    if shorter != query:
        rungs.append(Rung(f"Papers on {shorter}", {"query": shorter, "sources": ["scholar-articles"]}))
    return rungs


def pills(body: dict[str, Any]) -> list[PillDef]:
    two_years_ago = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=730)).timestamp())
    return [
        PillDef(
            "authors", "Top authors", "Switches the source to scholar-author.",
            "/web/search/live", {**body, "sources": ["scholar-author"]}, "authors",
        ),
        PillDef(
            "recent", "Last two years", "Adds a start date.",
            "/web/search/live", {**body, "start_date": two_years_ago}, "papers",
        ),
    ]


def paper_rows(data: Any) -> list[dict[str, Any]]:
    rows = []
    for result in dig(data, "results", default=[]) or []:
        if not isinstance(result, dict):
            continue
        authors = ", ".join(a.get("name", "") for a in result.get("authors") or [] if isinstance(a, dict))
        citations = result.get("citations")
        rows.append({
            "name": authors or dig(result, "metadata", default="") or "Unknown author",
            "title": result.get("title") or "",
            "company": f"{citations} citations" if isinstance(citations, int) else "",
            "url": result.get("url") or "",
            "start_date": "",
            "updated_at": "",
        })
    return [r for r in rows if r["title"]]


def author_rows(data: Any) -> list[dict[str, Any]]:
    rows = []
    for result in dig(data, "results", default=[]) or []:
        if not isinstance(result, dict) or not result.get("name"):
            continue
        rows.append({
            "name": result["name"],
            "title": ", ".join(i.get("title", "") for i in (result.get("interests") or [])[:3] if isinstance(i, dict)),
            "company": result.get("affiliation") or "",
            "url": result.get("url") or "",
            "start_date": "",
            "updated_at": "",
        })
    return rows
