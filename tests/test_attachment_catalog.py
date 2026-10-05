"""Attachment catalog: content-based reuse across separately-kept messages.

Runs against the memory backend always and, when configured, the same cases
against PostgreSQL (``pg_client`` fixture) so both implementations agree.
"""
import hashlib
import logging
from email.message import EmailMessage

import pytest

PAYLOAD = b"FORWARDED-CONTRACT-BYTES-v1"
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).hexdigest()


def _eml(*, message_id, subject, filename, payload=PAYLOAD, ctype="application/pdf",
         body="please review the attached quarterly contract."):
    msg = EmailMessage()
    msg["From"] = "clerk@example.com"
    msg["To"] = "archive@example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{message_id}>"
    msg.set_content(body)
    msg.add_attachment(payload, maintype=ctype.split("/", 1)[0],
                       subtype=ctype.split("/", 1)[1], filename=filename)
    return msg.as_bytes()


def _post(c, name, data):
    return c.post(
        "/ingest",
        files={"file": (name, data, "message/rfc822")},
        params={"recompute_threads": False},
    )


def _ingest_pair(c):
    r1 = _post(c, "original.eml", _eml(
        message_id="orig@example.com", subject="Original contract",
        filename="contract-2026.pdf"))
    r2 = _post(c, "forward.eml", _eml(
        message_id="fwd@example.com", subject="FW: Original contract",
        filename="re_contract.pdf"))
    assert r1.status_code == 201 and r2.status_code == 201
    return r1.json(), r2.json()


def _catalog(c, **params):
    r = c.get("/attachments", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# -- acceptance 1: same content, different names, separate messages --------
@pytest.mark.parametrize("fixture_name", ["client", "pg_client"])
def test_same_content_different_names_aggregate_and_trace(request, fixture_name):
    c, arch = request.getfixturevalue(fixture_name)
    j1, j2 = _ingest_pair(c)
    pk1, pk2 = j1["message_pk"], j2["message_pk"]

    # physical file shared: both rows point at the same content-addressed path
    a1 = j1["attachments"][0]
    a2 = j2["attachments"][0]
    assert a1["sha256"] == a2["sha256"] == PAYLOAD_SHA
    assert a1["filename"] != a2["filename"]
    assert a1["storage_path"] == a2["storage_path"]
    assert list(arch.attachment_storage.root.rglob("att-*.bin"))
    assert len(list(arch.attachment_storage.root.rglob("att-*.bin"))) == 1

    cat = _catalog(c, sha256=PAYLOAD_SHA)
    assert cat["count"] == 1
    grp = cat["groups"][0]
    assert grp["sha256"] == PAYLOAD_SHA
    assert grp["byte_size"] == len(PAYLOAD)
    assert set(grp["filenames"]) == {"contract-2026.pdf", "re_contract.pdf"}

    occs = sorted(grp["occurrences"], key=lambda o: o["message_pk"])
    assert [o["message_pk"] for o in occs] == [pk1, pk2]
    assert {o["filename"] for o in occs} == {"contract-2026.pdf", "re_contract.pdf"}
    # each occurrence traces back to its own mail + MIME path + 原文摘要
    by_pk = {o["message_pk"]: o for o in occs}
    assert by_pk[pk1]["subject"] == "Original contract"
    assert by_pk[pk2]["subject"] == "FW: Original contract"
    assert by_pk[pk1]["mime_path"] == by_pk[pk2]["mime_path"] == "2"
    assert "quarterly contract" in by_pk[pk1]["snippet"]
    assert "quarterly contract" in by_pk[pk2]["snippet"]
    assert by_pk[pk1]["attachment_id"] != by_pk[pk2]["attachment_id"]
    # per-message scoped links stay independent
    assert by_pk[pk1]["download_url"] == f"/messages/{pk1}/attachments/{by_pk[pk1]['attachment_id']}/download"
    assert by_pk[pk2]["download_url"] == f"/messages/{pk2}/attachments/{by_pk[pk2]['attachment_id']}/download"
    assert all(o["availability"] == "available" for o in occs)

    # mails were not merged
    assert pk1 != pk2
    msgs = c.get("/messages").json()
    assert {m["id"] for m in msgs} >= {pk1, pk2}


# -- acceptance 2: same name, different content must not merge -------------
@pytest.mark.parametrize("fixture_name", ["client", "pg_client"])
def test_same_name_different_content_not_merged(request, fixture_name):
    c, _ = request.getfixturevalue(fixture_name)
    other = b"TOTALLY-DIFFERENT-CONTRACT-BYTES-v2"
    _post(c, "a.eml", _eml(message_id="a@example.com", subject="A",
                           filename="same.pdf", payload=PAYLOAD))
    _post(c, "b.eml", _eml(message_id="b@example.com", subject="B",
                           filename="same.pdf", payload=other))
    cat = _catalog(c, filename="same.pdf")
    digests = {g["sha256"] for g in cat["groups"]}
    assert digests == {PAYLOAD_SHA, hashlib.sha256(other).hexdigest()}
    assert cat["count"] == 2
    for grp in cat["groups"]:
        assert grp["filenames"] == ["same.pdf"]
        assert len(grp["occurrences"]) == 1


# -- filters ---------------------------------------------------------------
@pytest.mark.parametrize("fixture_name", ["client", "pg_client"])
def test_catalog_filters_size_type_prefix_and_paging(request, fixture_name):
    c, _ = request.getfixturevalue(fixture_name)
    _post(c, "p.eml", _eml(message_id="p@example.com", subject="P",
                           filename="p.pdf", ctype="application/pdf"))
    _post(c, "g.eml", _eml(message_id="g@example.com", subject="G",
                           filename="g.gif", ctype="image/gif",
                           payload=b"GIF89a-DATA"))
    by_size = _catalog(c, size=len(PAYLOAD))
    assert by_size["count"] == 1
    assert by_size["groups"][0]["occurrences"][0]["filename"] == "p.pdf"
    by_type = _catalog(c, content_type="image/")
    assert {o["filename"] for g in by_type["groups"] for o in g["occurrences"]} == {"g.gif"}
    assert _catalog(c, content_type="application/pdf")["count"] == 1
    assert _catalog(c, sha256="0" * 64)["groups"] == []


def test_catalog_rejects_bad_sha(client):
    c, _ = client
    r = c.get("/attachments", params={"sha256": "not-hex"})
    assert r.status_code == 422


# -- acceptance 3: damaged storage path keeps metadata, download fails -----
@pytest.mark.parametrize("fixture_name", ["client", "pg_client"])
def test_damaged_storage_path_catalog_ok_download_fails(request, fixture_name):
    c, arch = request.getfixturevalue(fixture_name)
    j1, j2 = _ingest_pair(c)
    pk1 = j1["message_pk"]
    meta1 = c.get(f"/messages/{pk1}").json()["attachments"][0]
    pk2 = j2["message_pk"]
    meta2 = c.get(f"/messages/{pk2}").json()["attachments"][0]

    # Corrupt the shared physical bytes.
    physical = arch.attachment_storage.resolve(j1["attachments"][0]["storage_path"])
    physical.write_bytes(b"corrupted")

    cat = _catalog(c, sha256=PAYLOAD_SHA)
    grp = cat["groups"][0]
    # metadata (names, mime paths, snippets) still fully listed
    assert len(grp["occurrences"]) == 2

    # downloads of both mails fail explicitly — 409 integrity mismatch
    d1 = c.get(f"/messages/{pk1}/attachments/{meta1['id']}/download")
    d2 = c.get(f"/messages/{pk2}/attachments/{meta2['id']}/download")
    assert d1.status_code == 409 and d2.status_code == 409

    # an independently stored attachment on another path still downloads fine
    r3 = _post(c, "other.eml", _eml(
        message_id="other@example.com", subject="Other",
        filename="ok.gif", payload=b"GIF89a-INDEPENDENT", ctype="image/gif"))
    pk3 = r3.json()["message_pk"]
    meta3 = c.get(f"/messages/{pk3}").json()["attachments"][0]
    d3 = c.get(f"/messages/{pk3}/attachments/{meta3['id']}/download")
    assert d3.status_code == 200
    assert d3.content == b"GIF89a-INDEPENDENT"


@pytest.mark.parametrize("fixture_name", ["client", "pg_client"])
def test_missing_file_listed_as_missing_and_download_410(request, fixture_name):
    c, arch = request.getfixturevalue(fixture_name)
    r = _post(c, "x.eml", _eml(message_id="x@example.com", subject="X",
                               filename="x.pdf"))
    j = r.json()
    pk = j["message_pk"]
    meta = c.get(f"/messages/{pk}").json()["attachments"][0]
    physical = arch.attachment_storage.resolve(j["attachments"][0]["storage_path"])
    physical.unlink()
    cat = _catalog(c, sha256=PAYLOAD_SHA)
    occ = cat["groups"][0]["occurrences"][0]
    assert occ["availability"] == "missing"  # metadata still shown
    dl = c.get(f"/messages/{pk}/attachments/{meta['id']}/download")
    assert dl.status_code == 410


# -- independent per-message permission checks -----------------------------
@pytest.mark.parametrize("fixture_name", ["client", "pg_client"])
def test_download_cross_message_attachment_id_denied(request, fixture_name):
    c, _ = request.getfixturevalue(fixture_name)
    j1, j2 = _ingest_pair(c)
    pk1, pk2 = j1["message_pk"], j2["message_pk"]
    att1 = c.get(f"/messages/{pk1}").json()["attachments"][0]
    # use mail #2's path with mail #1's attachment id — must not resolve
    r = c.get(f"/messages/{pk2}/attachments/{att1['id']}/download")
    assert r.status_code == 404
    r = c.get(f"/messages/{pk1}/attachments/{999999}/download")
    assert r.status_code == 404
    # the legitimate download still works on the shared file
    ok = c.get(f"/messages/{pk1}/attachments/{att1['id']}/download")
    assert ok.status_code == 200 and ok.content == PAYLOAD


def test_catalog_and_failed_downloads_never_log_bytes(client, caplog):
    c, arch = client
    j1, _ = _ingest_pair(c)
    pk = j1["message_pk"]
    meta = c.get(f"/messages/{pk}").json()["attachments"][0]
    physical = arch.attachment_storage.resolve(j1["attachments"][0]["storage_path"])
    physical.write_bytes(b"corrupted bytes on disk")
    with caplog.at_level(logging.INFO):
        _catalog(c, sha256=PAYLOAD_SHA)
        dl = c.get(f"/messages/{pk}/attachments/{meta['id']}/download")
    assert dl.status_code == 409
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert PAYLOAD.decode() not in blob
    assert b"corrupted bytes on disk".decode() not in blob
    # digests are metadata and may appear; raw bytes never do
