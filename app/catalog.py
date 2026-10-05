"""Shared attachment catalog aggregation for repository implementations."""
from __future__ import annotations

import re
from typing import Any, Sequence

_WHITESPACE_RE = re.compile(r"\s+")
_SNIPPET_LENGTH = 200


def body_snippet(rows: Sequence[dict[str, Any]]) -> str | None:
    """Return a short source-message text excerpt without exposing attachment bytes."""
    plain_parts = [
        r for r in rows if r.get("content_type") == "text/plain" and r.get("plain_text")
    ]
    html_parts = [r for r in rows if r.get("content_type") == "text/html" and r.get("plain_text")]
    text_parts = [r for r in rows if r.get("text")]
    candidate = (
        (plain_parts[0]["plain_text"] if plain_parts else None)
        or (html_parts[0]["plain_text"] if html_parts else None)
        or (text_parts[0]["text"] if text_parts else None)
    )
    if not candidate:
        return None
    candidate = _WHITESPACE_RE.sub(" ", candidate).strip()
    if not candidate:
        return None
    return candidate[:_SNIPPET_LENGTH].strip()


def build_attachment_catalog(occurrences: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group already-filtered attachment occurrences strictly by SHA-256."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in occurrences:
        grouped.setdefault(row["checksum_sha256"], []).append(row)

    groups: list[dict[str, Any]] = []
    for digest in sorted(grouped):
        rows = sorted(grouped[digest], key=lambda x: (x["message_pk"], x["attachment_id"]))
        shared_paths = sorted({r["storage_path"] for r in rows if r["storage_path"]})
        # A damaged or missing relationship still appears in the catalog; its
        # download endpoint performs an independent filesystem/path failure.
        groups.append(
            {
                "checksum_sha256": digest,
                "byte_size": rows[0]["byte_size"],
                "shared_storage_paths": shared_paths,
                "stored": all(r["stored"] for r in rows),
                "occurrence_count": len(rows),
                "occurrences": rows,
            }
        )
    return groups
