from attachment_catalog_cases import assert_attachment_catalog_behavior


def test_attachment_catalog_groups_by_content(client):
    c, arch = client
    assert_attachment_catalog_behavior(c, arch)
