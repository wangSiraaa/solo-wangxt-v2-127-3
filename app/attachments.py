"""Attachment catalog: content-based retrieval across messages.

The catalog identifies physical files **by content** (SHA-256) so the same
attachment forwarded in several mails can be located and reused without
merging the mails themselves. Grouping happens on the digest only — identical
filenames with different content always stay in separate groups, identical
content with different display names aggregates into one group while every
per-message occurrence (its own id, MIME path, filename and download link)
remains independently traceable.

Both repository backends produce the same flat "occurrence" rows; the
grouping/pagination shaping here is shared so the in-memory and PostgreSQL
implementations cannot drift.
"""
from __future__ import annotations

import re
from typing import Any

_SNIPPET_LEN = 280
_WS_RE = re.compile(r"\s+")


def make_snippet(text: str | None) -> str:
    """Collapse a stored body text into a bounded one-line 原文摘要."""
    if not text:
        return ""
    one_line = _WS_RE.sub(" ", text).strip()
    if len(one_line) > _SNIPPET_LEN:
        return one_line[:_SNIPPET_LEN] + "…"
    return one_line


def shape_catalog(
    rows: list[dict[str, Any]], *, limit: int, offset: int
) -> dict[str, Any]:
    """Group flat attachment rows by digest, then paginate over the groups.

    Each row must carry ``sha256`` and ``byte_size`` plus an ``occurrence``
    dict describing the owning message. Rows are expected pre-sorted by
    attachment id ascending, which fixes the order inside an occurrence list.
    """
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        digest = row["sha256"]
        grp = groups.get(digest)
        if grp is None:
            grp = {
                "sha256": digest,
                "byte_size": row["byte_size"],
                # Content types/names across the reuses: distinct values only,
                # so same-content/different-name mails aggregate yet preserve
                # every name they arrived under.
                "content_types": [],
                "filenames": [],
                "occurrences": [],
            }
            groups[digest] = grp
        if row.get("content_type") and row["content_type"] not in grp["content_types"]:
            grp["content_types"].append(row["content_type"])
        fname = row.get("filename")
        if fname is not None and fname not in grp["filenames"]:
            grp["filenames"].append(fname)
        grp["occurrences"].append(row["occurrence"])

    ordered = sorted(
        groups.values(),
        key=lambda g: min(o["attachment_id"] for o in g["occurrences"]),
        reverse=True,
    )
    page = ordered[offset : offset + limit]
    return {"count": len(ordered), "limit": limit, "offset": offset, "groups": page}
