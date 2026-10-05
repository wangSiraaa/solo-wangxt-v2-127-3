"""Application services: orchestrate parse, controlled storage, persistence."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.parser import ParseStatus, parse_eml
from app.repository import Repository
from app.storage import AttachmentMissingError, ControlledStorage

log = logging.getLogger("emlarchive.service")


@dataclass
class IngestResult:
    ingest_id: int
    message_pk: int | None
    status: str
    raw_sha256: str
    raw_size: int
    defects_count: int
    fatal_error: str | None
    attachments: list[dict[str, Any]]
    threads: dict[str, Any]


class IngestService:
    def __init__(self, repo: Repository, raw_storage: ControlledStorage, attachment_storage: ControlledStorage) -> None:
        self._repo = repo
        self._raw = raw_storage
        self._att = attachment_storage

    def ingest(self, data: bytes, *, source_name: str | None = None, recompute_threads: bool = True) -> IngestResult:
        # Parse first so even catastrophic failures have a digest.
        parsed = parse_eml(data)

        raw_relpath: str | None = None
        if parsed.status is not ParseStatus.FAILED:
            try:
                _, raw_relpath = self._raw.store_raw(data)
            except Exception as exc:  # storage failure must not be swallowed
                log.error("raw eml storage failed sha256=%s: %s", parsed.raw_sha256, exc)
                raise

        stored: list[tuple[Any, str]] = []
        att_summaries: list[dict[str, Any]] = []
        for att in parsed.attachments:
            if att.bytes_to_persist is None:
                continue
            try:
                rel = self._att.store_attachment(
                    att.bytes_to_persist,
                    att.checksum_sha256,
                    att.filename,
                )
            except Exception as exc:
                # Keep parse metadata but mark the attachment as not stored so
                # the failure is locatable; never log bytes.
                log.error(
                    "attachment storage failed part=%s content_type=%s bytes=%d sha256=%s: %s",
                    att.mime_path,
                    att.content_type,
                    att.byte_size,
                    att.checksum_sha256,
                    exc,
                )
                att_summaries.append(
                    {
                        "mime_path": att.mime_path,
                        "filename": att.filename,
                        "stored": False,
                        "error": str(exc),
                        "byte_size": att.byte_size,
                    }
                )
                continue
            stored.append((att, rel))
            att.storage_path = rel
            att.bytes_to_persist = None  # free memory; bytes are on disk now
            att_summaries.append(
                {
                    "mime_path": att.mime_path,
                    "filename": att.filename,
                    "content_type": att.content_type,
                    "stored": True,
                    "storage_path": rel,
                    "byte_size": att.byte_size,
                    "sha256": att.checksum_sha256,
                }
            )

        saved = self._repo.save_ingest(
            parsed,
            raw_relpath=raw_relpath,
            stored_attachments=stored,
            source_name=source_name,
            status=parsed.status.value,
            fatal_error=parsed.fatal_error,
        )

        threads: dict[str, Any] = {}
        if recompute_threads and saved.get("message_id") is not None:
            threads = self._repo.rebuild_threads()

        return IngestResult(
            ingest_id=saved["ingest_id"],
            message_pk=saved.get("message_id"),
            status=parsed.status.value,
            raw_sha256=parsed.raw_sha256 or "",
            raw_size=parsed.raw_size or len(data),
            defects_count=len(parsed.defects),
            fatal_error=parsed.fatal_error,
            attachments=att_summaries,
            threads=threads,
        )

    # -- attachment catalog / downloads ------------------------------------
    def _availability(self, occurrence: dict[str, Any]) -> str:
        """Cheap on-disk liveness for catalog rows (metadata only).

        Full size+SHA-256 verification happens at download time; a group whose
        physical file is damaged still lists, with each occurrence's state
        reported independently.
        """
        rel = occurrence.get("storage_path")
        if not rel:
            return "unstored"
        try:
            exists = self._att.exists(rel)
        except StorageError:
            return "invalid_path"
        return "available" if exists else "missing"

    def query_attachments(
        self,
        *,
        sha256: str | None = None,
        byte_size: int | None = None,
        content_type: str | None = None,
        filename: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        result = self._repo.query_attachments(
            sha256=sha256,
            byte_size=byte_size,
            content_type=content_type,
            filename=filename,
            limit=limit,
            offset=offset,
        )
        for grp in result["groups"]:
            for occ in grp["occurrences"]:
                # The physical file may be shared across the group, but the
                # access relation is per message: each occurrence gets its own
                # scoped download link and independently reported state.
                occ["availability"] = self._availability(occ)
                occ["download_url"] = (
                    f"/messages/{occ['message_pk']}/attachments/"
                    f"{occ['attachment_id']}/download"
                )
                # Internal routing detail, not part of the catalog contract.
                occ.pop("storage_path", None)
        return result

    def open_attachment_download(
        self, message_pk: int, attachment_id: int
    ) -> tuple[Any, dict[str, Any]]:
        """Resolve a scoped attachment download.

        The attachment must belong to this exact message — reusing another
        message's attachment id never grants access. Raises LookupError when
        the relation does not exist. The returned path has passed root
        re-validation and size/SHA-256 verification.
        """
        att = self._repo.get_attachment_by_message(message_pk, attachment_id)
        if att is None:
            raise LookupError("attachment not found")
        if not att.get("storage_path"):
            raise AttachmentMissingError("attachment was never stored")
        path = self._att.verify_attachment(
            att["storage_path"],
            expected_size=att["byte_size"],
            expected_sha256=att["checksum_sha256"],
        )
        return path, att
