import hashlib
from email.message import EmailMessage
from email.utils import formatdate


def make_eml(message_id, subject, body, payload, filename, content_type="text/plain"):
    msg = EmailMessage()
    msg["From"] = "archivist@example.com"
    msg["To"] = "archive@example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{message_id}>"
    msg["Date"] = formatdate()
    msg.set_content(body)
    maintype, _, subtype = content_type.partition("/")
    msg.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
    return msg.as_bytes()


def post(c, name, data):
    return c.post(
        "/ingest",
        files={"file": (name, data, "message/rfc822")},
    )


def assert_attachment_catalog_behavior(c, arch):
    shared = b"same forwarded attachment bytes"
    shared_sha = hashlib.sha256(shared).hexdigest()
    first = post(
        c,
        "forwarded-a.eml",
        make_eml(
            "forwarded-a@example.com",
            "Forwarded original A",
            "Original summary body A",
            shared,
            "original-name.txt",
        ),
    )
    second = post(
        c,
        "forwarded-b.eml",
        make_eml(
            "forwarded-b@example.com",
            "Forwarded copy B",
            "Original summary body B",
            shared,
            "renamed-copy.txt",
        ),
    )
    assert first.status_code == 201 and second.status_code == 201
    pk_a, pk_b = first.json()["message_pk"], second.json()["message_pk"]
    att_a = c.get(f"/messages/{pk_a}").json()["attachments"][0]
    att_b = c.get(f"/messages/{pk_b}").json()["attachments"][0]
    assert att_a["storage_path"] == att_b["storage_path"]
    assert arch.attachment_storage.resolve(att_a["storage_path"]).read_bytes() == shared

    catalog = c.get("/attachments", params={"sha256": shared_sha}).json()
    assert catalog["count"] == 1
    group = catalog["groups"][0]
    assert group["checksum_sha256"] == shared_sha
    assert group["byte_size"] == len(shared)
    assert group["occurrence_count"] == 2
    assert group["shared_storage_paths"] == [att_a["storage_path"]]
    occurrences = {(o["message_pk"], o["filename"]) for o in group["occurrences"]}
    assert occurrences == {(pk_a, "original-name.txt"), (pk_b, "renamed-copy.txt")}
    by_pk = {o["message_pk"]: o for o in group["occurrences"]}
    assert by_pk[pk_a]["mime_path"] == by_pk[pk_b]["mime_path"] == "2"
    assert by_pk[pk_a]["message_id"] == "forwarded-a@example.com"
    assert by_pk[pk_b]["message_id"] == "forwarded-b@example.com"
    assert by_pk[pk_a]["source_snippet"] == "Original summary body A"
    assert by_pk[pk_b]["source_snippet"] == "Original summary body B"

    # Filters all independently identify the same content relationship.
    assert c.get(
        "/attachments",
        params={"size": len(shared), "type": "text/plain", "filename": "original-name.txt"},
    ).json()["count"] == 1
    assert c.get(
        "/attachments", params={"filename": "renamed-copy.txt"}
    ).json()["groups"][0]["occurrences"][0]["message_pk"] == pk_b

    # Physical bytes are shared, but every mail-scoped download is checked alone.
    download_a = f"/messages/{pk_a}/attachments/{att_a['id']}/download"
    download_b = f"/messages/{pk_b}/attachments/{att_b['id']}/download"
    assert c.get(download_a).content == shared
    assert c.get(download_b).content == shared
    assert c.get(f"/messages/{pk_a}/attachments/{att_b['id']}/download").status_code == 404
    assert c.get(f"/messages/{pk_b}/attachments/{att_a['id']}/download").status_code == 404

    # Same display name must never be used as content identity.
    alpha, beta = b"catalog alpha contents", b"catalog beta contents"
    post(c, "same-name-a.eml", make_eml(
        "same-name-a@example.com", "Same name A", "body alpha", alpha, "report.txt"
    ))
    post(c, "same-name-b.eml", make_eml(
        "same-name-b@example.com", "Same name B", "body beta", beta, "report.txt"
    ))
    same_name = c.get("/attachments", params={"filename": "report.txt"}).json()
    assert {g["checksum_sha256"] for g in same_name["groups"]} == {
        hashlib.sha256(alpha).hexdigest(),
        hashlib.sha256(beta).hexdigest(),
    }
    assert c.get(
        "/attachments", params={"sha256": hashlib.sha256(alpha).hexdigest()}
    ).json()["count"] == 1

    # Metadata remains catalog-readable when one relationship's stored path is bad.
    if hasattr(arch.repo, "attachments"):
        stored_rows = [
            a
            for a in arch.repo.attachments
            if a["checksum_sha256"] == shared_sha and a["message_pk"] == pk_a
        ]
        stored_rows[0]["storage_path"] = "missing/corrupt-catalog-path.bin"
    else:
        import psycopg

        with psycopg.connect(arch.repo._dsn, autocommit=True) as conn:
            conn.execute(
                """
                UPDATE attachments
                SET storage_path = %s
                WHERE checksum_sha256 = %s AND message_pk = %s
                """,
                ("missing/corrupt-catalog-path.bin", shared_sha, pk_a),
            )
    damaged_catalog = c.get("/attachments", params={"sha256": shared_sha}).json()
    assert damaged_catalog["count"] == 1
    assert damaged_catalog["groups"][0]["occurrence_count"] == 2
    assert c.get(download_a).status_code == 410
    assert c.get(download_b).content == shared
