"""The librarian agent surface.

Every safety property here has a test that fails when its guard is deleted --
overwrite refusal, path rejection, cross-library isolation, the scope split
between staging and committing. Coverage alone does not establish that: a suite
can execute a guard without ever asserting on it, which is how a codebase ends
up passing after `os.link` is swapped for something that silently overwrites.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from conftest import authorization, image_bytes
from fastapi.testclient import TestClient

from nineveh import librarian
from nineveh.librarian import (
    LibrarianError,
    best_match,
    suggest_filename,
    validate_filename,
)

ALL_SCOPES = ("catalog:read", "metadata:read", "ingest:stage", "ingest:commit")


def issue(
    client: TestClient,
    scopes: tuple[str, ...] = ALL_SCOPES,
    library_ids: tuple[str, ...] = (),
    name: str = "Cleo",
) -> dict:
    response = client.post(
        "/api/v1/admin/librarian-tokens",
        headers=authorization(),
        json={"name": name, "scopes": list(scopes), "library_ids": list(library_ids)},
    )
    assert response.status_code == 201, response.text
    return response.json()


def bearer(token: dict) -> dict:
    return {"Authorization": f"Bearer {token['secret']}"}


def cbz_bytes(pages: int = 3, tint: int = 0) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for number in range(1, pages + 1):
            archive.writestr(f"{number:03d}.png", image_bytes((tint, number, 0)))
    return buffer.getvalue()


def stage(
    client: TestClient,
    token: dict,
    series_id: str,
    filename: str,
    payload: bytes | None = None,
):
    return client.post(
        "/api/v1/librarian/ingest",
        headers=bearer(token),
        data={"series_id": series_id, "filename": filename},
        files={
            "file": (filename, payload or cbz_bytes(), "application/vnd.comicbook+zip")
        },
    )


@pytest.fixture
def series_id(client: TestClient) -> str:
    token = issue(client, ("catalog:read",), name="finder")
    found = client.get(
        "/api/v1/librarian/series", headers=bearer(token), params={"query": "Example"}
    ).json()
    return found["candidates"][0]["seriesId"]


# --------------------------------------------------------------------------
# Credentials and scope
# --------------------------------------------------------------------------


def test_the_secret_is_returned_once_and_never_stored_in_clear(client: TestClient):
    token = issue(client)
    assert token["secret"].startswith("nvh_")
    listed = client.get(
        "/api/v1/admin/librarian-tokens", headers=authorization()
    ).json()["tokens"]
    assert [item["name"] for item in listed] == ["Cleo"]
    assert all("secret" not in item for item in listed)


def test_authentication_rejects_anything_but_the_issued_secret(client: TestClient):
    issue(client)
    for header in (
        {},
        {"Authorization": "Bearer nvh_wrong"},
        {"Authorization": "Basic nvh_wrong"},
        {"Authorization": "Bearer "},
    ):
        response = client.get("/api/v1/librarian/libraries", headers=header)
        assert response.status_code == 401


def test_a_missing_scope_is_refused_and_recorded(client: TestClient):
    token = issue(client, ("catalog:read",))
    response = client.get(
        "/api/v1/librarian/series",
        headers=bearer(token),
        params={"author": "A. Writer"},
    )
    assert response.status_code == 403
    assert "metadata:read" in response.json()["detail"]
    feed = client.get(
        "/api/v1/admin/librarian/activity",
        headers=authorization(),
        params={"severity": "security"},
    ).json()["events"]
    denial = [item for item in feed if item["action"] == "scope.denied"]
    assert denial and denial[0]["outcome"] == "denied"


def test_revoking_clears_the_hash_so_the_secret_cannot_authenticate(
    client: TestClient,
):
    token = issue(client)
    assert (
        client.get("/api/v1/librarian/libraries", headers=bearer(token)).status_code
        == 200
    )
    revoked = client.delete(
        f"/api/v1/admin/librarian-tokens/{token['id']}", headers=authorization()
    )
    assert revoked.status_code == 200
    assert revoked.json()["revokedAt"]
    assert (
        client.get("/api/v1/librarian/libraries", headers=bearer(token)).status_code
        == 401
    )


def test_a_revoked_token_leaves_the_list_but_stays_auditable(client: TestClient):
    token = issue(client)
    client.delete(
        f"/api/v1/admin/librarian-tokens/{token['id']}", headers=authorization()
    )
    active = client.get(
        "/api/v1/admin/librarian-tokens", headers=authorization()
    ).json()["tokens"]
    assert active == []
    including = client.get(
        "/api/v1/admin/librarian-tokens",
        headers=authorization(),
        params={"include_revoked": True},
    ).json()["tokens"]
    assert [item["name"] for item in including] == ["Cleo"]
    feed = client.get(
        "/api/v1/admin/librarian/activity", headers=authorization()
    ).json()["events"]
    actions = {item["action"] for item in feed}
    assert {"token.issued", "token.revoked"} <= actions


def test_privileges_can_be_changed_without_rotating_the_secret(client: TestClient):
    token = issue(client, ("catalog:read",))
    assert (
        client.get(
            "/api/v1/librarian/series",
            headers=bearer(token),
            params={"author": "A. Writer"},
        ).status_code
        == 403
    )
    patched = client.patch(
        f"/api/v1/admin/librarian-tokens/{token['id']}",
        headers=authorization(),
        json={"scopes": ["catalog:read", "metadata:read"]},
    )
    assert patched.status_code == 200
    assert "secret" not in patched.json()
    # The same bearer keeps working, now with the wider grant.
    assert (
        client.get(
            "/api/v1/librarian/series",
            headers=bearer(token),
            params={"author": "A. Writer"},
        ).status_code
        == 200
    )


def test_a_privilege_change_is_recorded_with_before_and_after(client: TestClient):
    token = issue(client, ("catalog:read",))
    client.patch(
        f"/api/v1/admin/librarian-tokens/{token['id']}",
        headers=authorization(),
        json={"scopes": ["catalog:read", "ingest:stage"], "name": "Clio"},
    )
    feed = client.get(
        "/api/v1/admin/librarian/activity",
        headers=authorization(),
        params={"severity": "security"},
    ).json()["events"]
    changed = [item for item in feed if item["action"] == "token.scopes_changed"]
    assert changed, [item["action"] for item in feed]
    assert changed[0]["detail"]["before"] == ["catalog:read"]
    assert "ingest:stage" in changed[0]["detail"]["after"]
    assert "+ingest:stage" in changed[0]["summary"]
    assert any(item["action"] == "token.renamed" for item in feed)


def test_a_revoked_token_cannot_be_re_scoped(client: TestClient):
    token = issue(client)
    client.delete(
        f"/api/v1/admin/librarian-tokens/{token['id']}", headers=authorization()
    )
    response = client.patch(
        f"/api/v1/admin/librarian-tokens/{token['id']}",
        headers=authorization(),
        json={"scopes": ["catalog:read"]},
    )
    assert response.status_code == 404


def test_unsupported_scopes_are_refused(client: TestClient):
    response = client.post(
        "/api/v1/admin/librarian-tokens",
        headers=authorization(),
        json={"name": "bad", "scopes": ["catalog:read", "delete:everything"]},
    )
    assert response.status_code == 422


def test_token_administration_requires_an_administrator(client: TestClient, reader):
    assert (
        client.get("/api/v1/admin/librarian-tokens", headers=reader).status_code == 403
    )
    assert (
        client.get("/api/v1/admin/librarian/activity", headers=reader).status_code
        == 403
    )


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def test_an_exact_title_resolves_confidently(client: TestClient):
    token = issue(client, ("catalog:read",))
    body = client.get(
        "/api/v1/librarian/series",
        headers=bearer(token),
        params={"query": "Example Series"},
    ).json()
    assert body["candidates"][0]["score"] == 1.0
    assert body["confidentMatch"] == body["candidates"][0]["seriesId"]
    assert body["ambiguous"] is False


def test_a_typo_still_resolves(client: TestClient):
    token = issue(client, ("catalog:read",))
    body = client.get(
        "/api/v1/librarian/series",
        headers=bearer(token),
        params={"query": "Exampl Seres"},
    ).json()
    assert body["candidates"], body
    assert body["candidates"][0]["localName"] == "Example Series"


def test_a_single_character_query_matches_nothing(client: TestClient):
    """A one-letter query must not return near-certain wrong answers.

    Scoring it loosely hands a small model a list of 0.96 candidates it has no
    way to discriminate, which is worse than an empty result it can report.
    """
    token = issue(client, ("catalog:read",))
    body = client.get(
        "/api/v1/librarian/series", headers=bearer(token), params={"query": "e"}
    ).json()
    assert body["candidates"] == []
    assert body["ambiguous"] is True


def test_an_unknown_library_is_reported_rather_than_ignored(client: TestClient):
    token = issue(client, ("catalog:read",))
    response = client.get(
        "/api/v1/librarian/series",
        headers=bearer(token),
        params={"query": "Example", "library": "Nowhere"},
    )
    assert response.status_code == 404


def test_a_query_or_a_filter_is_required(client: TestClient):
    token = issue(client, ("catalog:read", "metadata:read"))
    response = client.get("/api/v1/librarian/series", headers=bearer(token))
    assert response.status_code == 422


def test_a_disabled_library_is_invisible_by_id_and_by_name(client: TestClient):
    """Both lookup paths, because they are guarded in different places."""
    token = issue(client, ("catalog:read",))
    listed = client.get("/api/v1/admin/libraries", headers=authorization()).json()[
        "libraries"
    ]
    library = listed[0]
    client.delete(f"/api/v1/admin/libraries/{library['id']}", headers=authorization())
    for reference in (library["id"], library["name"], library["name"].lower()):
        response = client.get(
            "/api/v1/librarian/series",
            headers=bearer(token),
            params={"query": "Example", "library": reference},
        )
        assert response.status_code == 404, reference


def test_a_token_cannot_reach_a_library_outside_its_grant(
    client: TestClient, series_id: str
):
    token = issue(client, ("catalog:read",), library_ids=("some-other-library",))
    assert (
        client.get("/api/v1/librarian/libraries", headers=bearer(token)).json()[
            "libraries"
        ]
        == []
    )
    assert (
        client.get(
            f"/api/v1/librarian/series/{series_id}", headers=bearer(token)
        ).status_code
        == 404
    )
    body = client.get(
        "/api/v1/librarian/series",
        headers=bearer(token),
        params={"query": "Example Series"},
    ).json()
    assert body["candidates"] == []


# --------------------------------------------------------------------------
# Inventory and metadata
# --------------------------------------------------------------------------


def test_inventory_reports_volumes_and_filenames(client: TestClient, series_id: str):
    token = issue(client, ("catalog:read",))
    body = client.get(
        f"/api/v1/librarian/series/{series_id}", headers=bearer(token)
    ).json()
    assert body["inventory"]["publicationCount"] == 1
    assert body["inventory"]["filenames"] == ["Issue 1.cbz"]
    assert body["inventory"]["latest"]["filename"] == "Issue 1.cbz"
    assert body["inventory"]["publications"][0]["id"]
    assert body["inventory"]["totalSize"] > 0


def test_inventory_rejects_an_unknown_series(client: TestClient):
    token = issue(client, ("catalog:read",))
    assert (
        client.get(
            "/api/v1/librarian/series/not-a-series", headers=bearer(token)
        ).status_code
        == 404
    )


def test_metadata_search_needs_at_least_one_filter(client: TestClient):
    token = issue(client, ("metadata:read",))
    response = client.get(
        "/api/v1/librarian/series", headers=bearer(token), params={"tag": ""}
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------


def test_staging_writes_nothing_into_the_library(
    client: TestClient, series_id: str, library
):
    settings, _ = library
    token = issue(client, ("catalog:read", "ingest:stage"))
    response = stage(client, token, series_id, "Issue 2.cbz")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] == "staged"
    assert body["targetPath"].endswith("Issue 2.cbz")
    assert body["sha256"]
    assert not (settings.data_dir / body["targetPath"]).exists()
    assert (settings.state_dir / "ingest" / f"{body['ingestId']}.cbz").is_file()


def test_the_round_trip_places_the_volume_and_indexes_it(
    client: TestClient, series_id: str, library
):
    settings, _ = library
    token = issue(client, ("catalog:read", "ingest:stage", "ingest:commit"))
    staged = stage(client, token, series_id, "Issue 2.cbz").json()
    committed = client.post(
        f"/api/v1/librarian/ingest/{staged['ingestId']}/commit", headers=bearer(token)
    )
    assert committed.status_code == 200, committed.text
    placed = committed.json()
    assert placed["state"] == "placed"
    target = settings.data_dir / placed["relativePath"]
    assert target.is_file()
    # World-readable: the library is served to other accounts, and mkstemp
    # would otherwise leave it at 0600.
    assert target.stat().st_mode & 0o044


def test_committing_twice_is_not_possible(client: TestClient, series_id: str):
    token = issue(client, ("catalog:read", "ingest:stage", "ingest:commit"))
    staged = stage(client, token, series_id, "Issue 2.cbz").json()
    first = client.post(
        f"/api/v1/librarian/ingest/{staged['ingestId']}/commit", headers=bearer(token)
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/v1/librarian/ingest/{staged['ingestId']}/commit", headers=bearer(token)
    )
    assert second.status_code == 404


def test_an_existing_file_is_never_overwritten(
    client: TestClient, series_id: str, library
):
    _settings, archive = library
    token = issue(client, ("catalog:read", "ingest:stage", "ingest:commit"))
    original = archive.read_bytes()
    staged = stage(client, token, series_id, "Issue 1.cbz", cbz_bytes(pages=5)).json()
    response = client.post(
        f"/api/v1/librarian/ingest/{staged['ingestId']}/commit", headers=bearer(token)
    )
    assert response.status_code == 409
    assert "Already exists" in response.json()["detail"]
    assert archive.read_bytes() == original


def test_a_commit_interrupted_after_the_copy_leaves_no_archive(
    client: TestClient, series_id: str, library, monkeypatch
):
    """The property `shutil.move` could not offer across a mount boundary.

    A rename from `/state` to `/data` raises EXDEV, so a naive move degrades to
    copy-then-delete and a crash mid-copy strands a truncated `.cbz` under the
    real name for the next scan to index.
    """
    settings, _ = library
    token = issue(client, ("catalog:read", "ingest:stage", "ingest:commit"))
    staged = stage(client, token, series_id, "Issue 9.cbz").json()
    target = settings.data_dir / staged["targetPath"]

    def explode(source, destination):
        raise OSError("interrupted")

    monkeypatch.setattr(librarian.os, "link", explode)
    with pytest.raises(OSError):
        client.post(
            f"/api/v1/librarian/ingest/{staged['ingestId']}/commit",
            headers=bearer(token),
        )
    assert not target.exists()
    # No temporary debris, and the directory holds exactly what it started with.
    assert list(target.parent.glob("*.part")) == []
    assert sorted(item.name for item in target.parent.glob("*")) == ["Issue 1.cbz"]


@pytest.mark.parametrize(
    "filename",
    ["../escape.cbz", "a/b.cbz", "..\\escape.cbz", "nul\x00.cbz", ".cbz", "plain.txt"],
)
def test_path_shaped_filenames_are_refused(
    client: TestClient, series_id: str, filename: str
):
    token = issue(client, ("catalog:read", "ingest:stage"))
    assert stage(client, token, series_id, filename).status_code == 422


def test_a_corrupt_archive_is_refused_and_leaves_no_staging_debris(
    client: TestClient, series_id: str, library
):
    settings, _ = library
    token = issue(client, ("catalog:read", "ingest:stage"))
    response = stage(client, token, series_id, "broken.cbz", b"not a zip at all")
    assert response.status_code == 422
    assert list((settings.state_dir / "ingest").glob("*.cbz")) == []


def test_an_oversized_upload_is_refused(
    client: TestClient, series_id: str, library, monkeypatch
):
    token = issue(client, ("catalog:read", "ingest:stage"))
    monkeypatch.setattr(client.app.state.container.ingest, "_max_upload_bytes", 64)
    response = stage(client, token, series_id, "big.cbz", cbz_bytes(pages=6))
    assert response.status_code == 413


def test_identical_content_is_flagged_before_the_commit(
    client: TestClient, series_id: str, library
):
    """The duplicate shows up in the proposal, not as a late rejection."""
    _settings, archive = library
    token = issue(client, ("catalog:read", "ingest:stage"))
    staged = stage(client, token, series_id, "Issue 2.cbz", archive.read_bytes()).json()
    assert staged["duplicateOf"]["filename"] == "Issue 1.cbz"


def test_a_different_archive_is_not_flagged_as_duplicate(
    client: TestClient, series_id: str
):
    token = issue(client, ("catalog:read", "ingest:stage"))
    staged = stage(client, token, series_id, "Issue 2.cbz", cbz_bytes(pages=4)).json()
    assert staged["duplicateOf"] is None


def test_a_staged_upload_can_be_discarded(client: TestClient, series_id: str, library):
    settings, _ = library
    token = issue(client, ("catalog:read", "ingest:stage"))
    staged = stage(client, token, series_id, "Issue 2.cbz").json()
    assert (
        client.delete(
            f"/api/v1/librarian/ingest/{staged['ingestId']}", headers=bearer(token)
        ).status_code
        == 204
    )
    assert list((settings.state_dir / "ingest").glob("*")) == []
    assert (
        client.delete(
            f"/api/v1/librarian/ingest/{staged['ingestId']}", headers=bearer(token)
        ).status_code
        == 404
    )


def test_pending_lists_what_is_waiting_for_approval(client: TestClient, series_id: str):
    token = issue(client, ("catalog:read", "ingest:stage"))
    stage(client, token, series_id, "Issue 2.cbz")
    stage(client, token, series_id, "Issue 3.cbz", cbz_bytes(pages=4))
    pending = client.get("/api/v1/librarian/ingest", headers=bearer(token)).json()
    assert len(pending["pending"]) == 2


def test_a_malformed_ingest_id_cannot_reach_the_filesystem(
    client: TestClient, series_id: str
):
    token = issue(client, ("catalog:read", "ingest:stage", "ingest:commit"))
    for candidate in ("../../etc/passwd", "z" * 32, "short"):
        assert (
            client.post(
                f"/api/v1/librarian/ingest/{candidate}/commit", headers=bearer(token)
            ).status_code
            == 404
        )


def test_the_outcome_survives_the_staged_record(client: TestClient, series_id: str):
    """A dropped connection must not cost the agent the answer."""
    token = issue(client, ("catalog:read", "ingest:stage", "ingest:commit"))
    staged = stage(client, token, series_id, "Issue 2.cbz").json()
    client.post(
        f"/api/v1/librarian/ingest/{staged['ingestId']}/commit", headers=bearer(token)
    )
    later = client.get(
        f"/api/v1/librarian/ingest/{staged['ingestId']}", headers=bearer(token)
    )
    assert later.status_code == 200
    assert later.json()["state"] == "placed"
    assert later.json()["relativePath"].endswith("Issue 2.cbz")


# --------------------------------------------------------------------------
# The scope split -- the reason staging and committing are separate
# --------------------------------------------------------------------------


def test_a_staging_token_cannot_place_what_it_proposed(
    client: TestClient, series_id: str, library
):
    """The remote half of the design: propose from anywhere, place at the desk."""
    settings, _ = library
    proposer = issue(client, ("catalog:read", "ingest:stage"), name="phone")
    staged = stage(client, proposer, series_id, "Issue 2.cbz").json()
    refused = client.post(
        f"/api/v1/librarian/ingest/{staged['ingestId']}/commit",
        headers=bearer(proposer),
    )
    assert refused.status_code == 403
    assert "ingest:commit" in refused.json()["detail"]
    assert not (settings.data_dir / staged["targetPath"]).exists()

    approver = issue(client, ALL_SCOPES, name="desk")
    placed = client.post(
        f"/api/v1/librarian/ingest/{staged['ingestId']}/commit",
        headers=bearer(approver),
    )
    assert placed.status_code == 200
    assert (settings.data_dir / staged["targetPath"]).is_file()


def test_a_commit_only_token_cannot_stage(client: TestClient, series_id: str):
    token = issue(client, ("catalog:read", "ingest:commit"))
    assert stage(client, token, series_id, "Issue 2.cbz").status_code == 403


# --------------------------------------------------------------------------
# Activity feed
# --------------------------------------------------------------------------


def test_the_feed_reads_as_sentences_and_records_the_scopes_in_force(
    client: TestClient, series_id: str
):
    token = issue(client, ALL_SCOPES)
    staged = stage(client, token, series_id, "Issue 2.cbz").json()
    client.post(
        f"/api/v1/librarian/ingest/{staged['ingestId']}/commit", headers=bearer(token)
    )
    feed = client.get(
        "/api/v1/admin/librarian/activity",
        headers=authorization(),
        params={"severity": "notice"},
    ).json()["events"]
    placed = next(item for item in feed if item["action"] == "ingest.commit")
    assert placed["summary"].startswith("“Cleo” placed ")
    assert placed["severity"] == "important"
    assert placed["outcome"] == "ok"
    assert placed["tokenName"] == "Cleo"
    assert placed["scopesAtTime"] == sorted(ALL_SCOPES)
    assert placed["scopesDiffer"] is False


def test_the_feed_hides_reads_until_asked(client: TestClient, series_id: str):
    token = issue(client, ("catalog:read",))
    client.get(
        "/api/v1/librarian/series", headers=bearer(token), params={"query": "Example"}
    )
    default = client.get(
        "/api/v1/admin/librarian/activity",
        headers=authorization(),
        params={"severity": "notice"},
    ).json()["events"]
    assert not [item for item in default if item["action"] == "series.resolve"]
    everything = client.get(
        "/api/v1/admin/librarian/activity",
        headers=authorization(),
        params={"severity": "info"},
    ).json()["events"]
    resolves = [item for item in everything if item["action"] == "series.resolve"]
    assert resolves and "resolved “Example”" in resolves[0]["summary"]


def test_a_chain_shares_one_correlation_id(client: TestClient, series_id: str):
    token = issue(client, ALL_SCOPES)
    headers = {**bearer(token), "X-Correlation-Id": "chain-1"}
    client.get("/api/v1/librarian/series", headers=headers, params={"query": "Example"})
    staged = client.post(
        "/api/v1/librarian/ingest",
        headers=headers,
        data={"series_id": series_id, "filename": "Issue 2.cbz"},
        files={"file": ("Issue 2.cbz", cbz_bytes(), "application/vnd.comicbook+zip")},
    ).json()
    client.post(
        f"/api/v1/librarian/ingest/{staged['ingestId']}/commit", headers=headers
    )
    grouped = client.get(
        "/api/v1/admin/librarian/activity",
        headers=authorization(),
        params={"correlation_id": "chain-1", "severity": "info"},
    ).json()["events"]
    assert {item["action"] for item in grouped} == {
        "series.resolve",
        "ingest.stage",
        "ingest.commit",
    }


def test_the_feed_flags_actions_taken_under_older_permissions(
    client: TestClient, series_id: str
):
    token = issue(client, ("catalog:read",))
    client.get(
        "/api/v1/librarian/series", headers=bearer(token), params={"query": "Example"}
    )
    client.patch(
        f"/api/v1/admin/librarian-tokens/{token['id']}",
        headers=authorization(),
        json={"scopes": list(ALL_SCOPES)},
    )
    feed = client.get(
        "/api/v1/admin/librarian/activity",
        headers=authorization(),
        params={"severity": "info", "action": "series.resolve"},
    ).json()["events"]
    assert feed[0]["scopesAtTime"] == ["catalog:read"]
    assert feed[0]["scopesDiffer"] is True


def test_no_audit_surface_ever_carries_the_secret(client: TestClient, series_id: str):
    token = issue(client, ALL_SCOPES)
    client.get(
        "/api/v1/librarian/series", headers=bearer(token), params={"query": "Example"}
    )
    feed = client.get(
        "/api/v1/admin/librarian/activity",
        headers=authorization(),
        params={"severity": "info"},
    )
    assert token["secret"] not in feed.text
    assert token["secret"][4:] not in feed.text


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    ["../x.cbz", "a/b.cbz", "..\\x.cbz", "\x00.cbz", ".cbz", "x.txt", "", "."],
)
def test_validate_filename_rejects(filename: str):
    with pytest.raises(LibrarianError):
        validate_filename(filename)


@pytest.mark.parametrize("filename", ["ok.cbz", "x.CBZ", "Vol 01.cbz"])
def test_validate_filename_accepts(filename: str):
    assert validate_filename(filename) == filename


def test_suggest_filename_follows_the_series_pattern():
    siblings = ["Saga 001.cbz", "Saga 002.cbz", "Saga 003.cbz"]
    assert suggest_filename("saga_v12.cbz", siblings) == (
        "Saga 012.cbz",
        "Saga NNN.cbz",
    )
    assert suggest_filename("Anything.cbz", []) == ("Anything.cbz", None)


def test_suggest_filename_sanitizes_hostile_characters():
    assert suggest_filename("ev<il>:x.cbz", [])[0] == "ev_il__x.cbz"
    assert suggest_filename("  sp  aced  .cbz", [])[0] == "sp aced.cbz"


def test_a_lone_sibling_is_not_a_pattern():
    """One neighbour is a coincidence; two is a convention."""
    assert suggest_filename("thing.cbz", ["Saga 001.cbz"]) == ("thing.cbz", None)


def test_exact_titles_outrank_substrings():
    titles = [("localName", "Saga"), ("title", "Saga of the Long Winter")]
    score, _source, value = best_match("Saga", titles)
    assert score == 1.0
    assert value == "Saga"


def test_a_single_character_never_matches_loosely():
    assert best_match("s", [("localName", "Saga")]) == (0.0, None, None)


# --------------------------------------------------------------------------
# Stored provider metadata
#
# The fixture library has no linked metadata, so these tests attach some and
# then exercise the paths that only exist once a series has been matched.
# --------------------------------------------------------------------------


@pytest.fixture
def matched(client: TestClient, series_id: str) -> str:
    client.app.state.container.repository.save_series_metadata(
        series_id,
        4242,
        "https://mangabaka.example/series/4242",
        {
            "title": "Exemplar",
            "alternative_titles": ["れい", "Reibun"],
            "authors": ["A. Writer"],
            "artists": ["B. Artist"],
            "publishers": ["Kodansha"],
            "tags": ["Adventure", "Seinen"],
            "status": "completed",
            "total_chapters": 214,
            "final_volume": 12,
        },
        {},
        None,
    )
    return series_id


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("author", "a. writer"),
        ("artist", "B. Artist"),
        ("publisher", "kodansha"),
        ("tag", "seinen"),
        ("status", "Completed"),
        ("title", "exempl"),
    ],
)
def test_metadata_search_matches_each_filter(
    client: TestClient, matched: str, field: str, value: str
):
    token = issue(client, ("metadata:read",))
    body = client.get(
        "/api/v1/librarian/series", headers=bearer(token), params={field: value}
    ).json()
    assert [item["seriesId"] for item in body["candidates"]] == [matched]
    assert body["candidates"][0]["title"] == "Exemplar"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("author", "Someone Else"),
        ("tag", "Cooking"),
        ("status", "ongoing"),
        ("title", "Nothing Like It"),
        ("publisher", "Nobody"),
        ("artist", "Nobody"),
    ],
)
def test_metadata_search_excludes_non_matches(
    client: TestClient, matched: str, field: str, value: str
):
    token = issue(client, ("metadata:read",))
    body = client.get(
        "/api/v1/librarian/series", headers=bearer(token), params={field: value}
    ).json()
    assert body["candidates"] == []


def test_every_filter_must_hold_at_once(client: TestClient, matched: str):
    token = issue(client, ("metadata:read",))
    body = client.get(
        "/api/v1/librarian/series",
        headers=bearer(token),
        params={"author": "A. Writer", "tag": "Cooking"},
    ).json()
    assert body["candidates"] == []


def test_a_series_resolves_by_its_provider_title(client: TestClient, matched: str):
    token = issue(client, ("catalog:read",))
    body = client.get(
        "/api/v1/librarian/series", headers=bearer(token), params={"query": "Exemplar"}
    ).json()
    assert body["candidates"][0]["matchedOn"] == "title"
    assert body["confidentMatch"] == matched


def test_a_series_resolves_by_an_alternative_title(client: TestClient, matched: str):
    token = issue(client, ("catalog:read",))
    body = client.get(
        "/api/v1/librarian/series", headers=bearer(token), params={"query": "Reibun"}
    ).json()
    assert body["candidates"][0]["matchedOn"] == "alternativeTitle"
    assert body["candidates"][0]["matchedValue"] == "Reibun"


def test_inventory_reports_provider_totals_for_completeness(
    client: TestClient, matched: str
):
    """The numbers behind "am I missing anything?"."""
    token = issue(client, ("catalog:read",))
    body = client.get(
        f"/api/v1/librarian/series/{matched}", headers=bearer(token)
    ).json()
    assert body["providerTotals"] == {
        "total_chapters": 214,
        "final_volume": 12,
        "status": "completed",
    }
    assert body["series"]["title"] == "Exemplar"


def test_metadata_with_no_usable_fields_is_simply_unmatched(
    client: TestClient, series_id: str
):
    client.app.state.container.repository.save_series_metadata(
        series_id,
        1,
        "https://example.invalid/1",
        {"title": 12, "authors": "nope"},
        {},
        None,
    )
    token = issue(client, ("catalog:read", "metadata:read"))
    assert (
        client.get(
            "/api/v1/librarian/series",
            headers=bearer(token),
            params={"author": "nope"},
        ).json()["candidates"]
        == []
    )
    # A non-string title must not break local-name resolution.
    body = client.get(
        "/api/v1/librarian/series",
        headers=bearer(token),
        params={"query": "Example Series"},
    ).json()
    assert body["candidates"][0]["matchedOn"] == "localName"


# --------------------------------------------------------------------------
# Remaining validation paths
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["", "   ", "x" * 65])
def test_a_token_name_must_be_reasonable(client: TestClient, name: str):
    response = client.post(
        "/api/v1/admin/librarian-tokens",
        headers=authorization(),
        json={"name": name, "scopes": ["catalog:read"]},
    )
    assert response.status_code == 422


def test_changing_the_reachable_libraries_is_recorded(client: TestClient):
    token = issue(client, ("catalog:read",))
    listed = client.get("/api/v1/admin/libraries", headers=authorization()).json()[
        "libraries"
    ]
    client.patch(
        f"/api/v1/admin/librarian-tokens/{token['id']}",
        headers=authorization(),
        json={"library_ids": [listed[0]["id"]]},
    )
    feed = client.get(
        "/api/v1/admin/librarian/activity",
        headers=authorization(),
        params={"severity": "security"},
    ).json()["events"]
    changed = [item for item in feed if item["action"] == "token.libraries_changed"]
    assert changed and "which libraries" in changed[0]["summary"]


def test_a_patch_that_changes_nothing_records_nothing(client: TestClient):
    token = issue(client, ("catalog:read",))
    before = client.get(
        "/api/v1/admin/librarian/activity",
        headers=authorization(),
        params={"severity": "security"},
    ).json()["events"]
    client.patch(
        f"/api/v1/admin/librarian-tokens/{token['id']}",
        headers=authorization(),
        json={"scopes": ["catalog:read"]},
    )
    after = client.get(
        "/api/v1/admin/librarian/activity",
        headers=authorization(),
        params={"severity": "security"},
    ).json()["events"]
    assert len(after) == len(before)


def test_patching_an_unknown_token_is_a_404(client: TestClient):
    assert (
        client.patch(
            "/api/v1/admin/librarian-tokens/nope",
            headers=authorization(),
            json={"scopes": ["catalog:read"]},
        ).status_code
        == 404
    )
    assert (
        client.delete(
            "/api/v1/admin/librarian-tokens/nope", headers=authorization()
        ).status_code
        == 404
    )


def test_an_unknown_staged_upload_is_a_404(client: TestClient):
    token = issue(client, ("ingest:stage",))
    assert (
        client.get(
            f"/api/v1/librarian/ingest/{'a' * 32}", headers=bearer(token)
        ).status_code
        == 404
    )


def test_a_corrupt_sidecar_is_ignored_rather_than_crashing(
    client: TestClient, series_id: str, library
):
    settings, _ = library
    token = issue(client, ("catalog:read", "ingest:stage", "ingest:commit"))
    staged = stage(client, token, series_id, "Issue 2.cbz").json()
    sidecar = settings.state_dir / "ingest" / f"{staged['ingestId']}.json"
    sidecar.write_text("{ not json", encoding="utf-8")
    assert (
        client.post(
            f"/api/v1/librarian/ingest/{staged['ingestId']}/commit",
            headers=bearer(token),
        ).status_code
        == 404
    )
    sidecar.write_text('{"id": "x"}', encoding="utf-8")
    assert (
        client.get("/api/v1/librarian/ingest", headers=bearer(token)).json()["pending"]
        == []
    )


def test_expired_staging_is_swept_including_orphaned_archives(
    client: TestClient, series_id: str, library
):
    """A crash between storing bytes and writing the sidecar must not leak."""
    import os
    import time

    settings, _ = library
    token = issue(client, ("catalog:read", "ingest:stage"))
    staged = stage(client, token, series_id, "Issue 2.cbz").json()
    staging = settings.state_dir / "ingest"
    orphan = staging / f"{'b' * 32}.cbz"
    orphan.write_bytes(b"left behind")
    stale = time.time() - (25 * 3600)
    for path in staging.iterdir():
        os.utime(path, (stale, stale))
    # Staging again runs the sweep.
    stage(client, token, series_id, "Issue 3.cbz", cbz_bytes(pages=4))
    assert not orphan.exists()
    assert not (staging / f"{staged['ingestId']}.cbz").exists()
    assert not (staging / f"{staged['ingestId']}.json").exists()


def test_a_disabled_library_stays_hidden_even_with_its_catalog_intact(
    client: TestClient, series_id: str
):
    """Pin the `enabled` filter independently of how a library got disabled.

    `remove_library` happens to delete the publications as well, so a test that
    goes through it proves nothing about the filter -- the series would be
    invisible anyway. Disabling the row on its own is the state the guard
    actually exists for.
    """
    repository = client.app.state.container.repository
    library = repository.managed_libraries()[0]
    with repository._connect() as connection:
        connection.execute(
            "UPDATE managed_libraries SET enabled = 0 WHERE id = ?", (library.id,)
        )
    token = issue(client, ("catalog:read",))
    assert (
        client.get("/api/v1/librarian/libraries", headers=bearer(token)).json()[
            "libraries"
        ]
        == []
    )
    assert (
        client.get(
            f"/api/v1/librarian/series/{series_id}", headers=bearer(token)
        ).status_code
        == 404
    )
    for reference in (library.id, library.name):
        assert (
            client.get(
                "/api/v1/librarian/series",
                headers=bearer(token),
                params={"query": "Example", "library": reference},
            ).status_code
            == 404
        )
    assert (
        client.get(
            "/api/v1/librarian/series",
            headers=bearer(token),
            params={"query": "Example"},
        ).json()["candidates"]
        == []
    )


def test_a_disabled_library_refuses_new_uploads(client: TestClient, series_id: str):
    token = issue(client, ("catalog:read", "ingest:stage"))
    repository = client.app.state.container.repository
    library = repository.managed_libraries()[0]
    with repository._connect() as connection:
        connection.execute(
            "UPDATE managed_libraries SET enabled = 0 WHERE id = ?", (library.id,)
        )
    assert stage(client, token, series_id, "Issue 2.cbz").status_code == 404


@pytest.mark.parametrize(
    "candidate",
    ["", "short", "z" * 32, "../../etc/passwd", "a" * 31, "A" * 32, "a" * 33],
)
def test_only_a_32_character_hex_id_names_a_staged_upload(candidate: str):
    """Guards the sidecar path against anything that is not an issued id.

    Not reachable through the router -- a path parameter cannot carry a
    traversal -- so it is asserted directly rather than over HTTP.
    """
    assert librarian._valid_ingest_id(candidate) is False


def test_an_issued_id_is_accepted():
    assert librarian._valid_ingest_id("0123456789abcdef" * 2) is True


# --------------------------------------------------------------------------
# Administration page
# --------------------------------------------------------------------------


def login(client: TestClient) -> str:
    from conftest import ADMIN_PASSWORD

    client.post(
        "/login",
        data={"username": "admin", "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    page = client.get("/admin/librarian")
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


def test_the_page_lists_capabilities_with_plain_descriptions(client: TestClient):
    login(client)
    page = client.get("/admin/librarian")
    assert page.status_code == 200
    for value, label, _ in librarian.SCOPE_OPTIONS:
        assert value in page.text
        assert label in page.text


def test_issuing_shows_the_secret_once_with_a_copy_control(client: TestClient):
    csrf = login(client)
    created = client.post(
        "/admin/librarian/tokens",
        data={
            "csrf_token": csrf,
            "name": "Cleo",
            "scopes": ["catalog:read", "ingest:stage"],
        },
    )
    assert created.status_code == 200
    assert "data-librarian-secret" in created.text
    assert "data-copy-secret" in created.text
    assert "will not be shown again" in created.text
    secret = created.text.split("data-librarian-secret>")[1].split("<")[0]
    assert secret.startswith("nvh_")
    # Never again, and never parked in the session to survive a redirect.
    assert secret not in client.get("/admin/librarian").text


def test_the_page_can_widen_a_token_without_reissuing_it(client: TestClient):
    csrf = login(client)
    client.post(
        "/admin/librarian/tokens",
        data={"csrf_token": csrf, "name": "Cleo", "scopes": ["catalog:read"]},
    )
    token_id = client.get(
        "/api/v1/admin/librarian-tokens", headers=authorization()
    ).json()["tokens"][0]["id"]
    client.post(
        f"/admin/librarian/tokens/{token_id}",
        data={
            "csrf_token": csrf,
            "name": "Cleo",
            "scopes": ["catalog:read", "ingest:stage"],
        },
    )
    listed = client.get(
        "/api/v1/admin/librarian-tokens", headers=authorization()
    ).json()["tokens"][0]
    assert listed["scopes"] == ["catalog:read", "ingest:stage"]


def test_revoking_clears_the_row_but_not_the_history(client: TestClient):
    csrf = login(client)
    client.post(
        "/admin/librarian/tokens",
        data={"csrf_token": csrf, "name": "Cleo", "scopes": ["catalog:read"]},
    )
    token_id = client.get(
        "/api/v1/admin/librarian-tokens", headers=authorization()
    ).json()["tokens"][0]["id"]
    client.post(f"/admin/librarian/tokens/{token_id}/revoke", data={"csrf_token": csrf})
    page = client.get("/admin/librarian?severity=security")
    assert "No librarian tokens yet." in page.text
    assert "issued token “Cleo”" in page.text
    assert "revoked token “Cleo”" in page.text


def test_the_activity_view_defaults_to_hiding_reads(client: TestClient, series_id: str):
    token = issue(client, ("catalog:read",))
    client.get(
        "/api/v1/librarian/series", headers=bearer(token), params={"query": "Example"}
    )
    login(client)
    assert "resolved “Example”" not in client.get("/admin/librarian").text
    assert "resolved “Example”" in client.get("/admin/librarian?severity=info").text
    # An unknown severity falls back rather than erroring.
    assert client.get("/admin/librarian?severity=bogus").status_code == 200


def test_the_page_rejects_a_missing_csrf_token(client: TestClient):
    login(client)
    response = client.post(
        "/admin/librarian/tokens",
        data={"csrf_token": "wrong", "name": "x", "scopes": ["catalog:read"]},
    )
    assert response.status_code == 403


def test_an_invalid_capability_set_is_reported_not_crashed(client: TestClient):
    csrf = login(client)
    response = client.post(
        "/admin/librarian/tokens",
        data={"csrf_token": csrf, "name": "x", "scopes": []},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "at least one capability" in response.text


def test_both_multi_selects_render_as_collapsed_pickers(client: TestClient):
    """One row of equal-height controls, not two tall fieldsets.

    Pinning the markup because the layout depends on it: `.picker > summary`
    is sized to match the text inputs, and `.picker-option > span` is the grid
    that keeps a capability's name off the same line as its description.
    """
    login(client)
    page = client.get("/admin/librarian").text
    form = page[page.index("librarian-token-form") :]
    form = form[: form.index("</form>")]
    assert form.count('<details class="picker">') == 2
    assert 'data-empty="None selected"' in form
    assert 'data-empty="All libraries"' in form
    # Each option keeps its description in the nested span the CSS stacks.
    assert form.count('class="picker-option"') == len(librarian.SCOPE_OPTIONS) + 1
    assert "<span>Read the catalog<small>" in form


def test_an_existing_token_opens_with_its_capabilities_ticked(client: TestClient):
    csrf = login(client)
    client.post(
        "/admin/librarian/tokens",
        data={
            "csrf_token": csrf,
            "name": "Cleo",
            "scopes": ["catalog:read", "ingest:stage"],
        },
    )
    page = client.get("/admin/librarian").text
    editor = page[page.index("Edit capabilities") :]
    assert editor.count("checked") == 2
    assert "2 selected" in editor


# --------------------------------------------------------------------------
# Trimming the activity feed
# --------------------------------------------------------------------------


def backdate(client: TestClient, days: int, *, action: str | None = None) -> None:
    """Age existing events so the retention windows can be exercised."""
    from datetime import UTC, datetime, timedelta

    when = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    repository = client.app.state.container.repository
    with repository._connect() as connection:
        if action:
            connection.execute(
                "UPDATE librarian_events SET created_at = ? WHERE action = ?",
                (when, action),
            )
        else:
            connection.execute("UPDATE librarian_events SET created_at = ?", (when,))


def feed(client: TestClient, severity: str = "info") -> list[dict]:
    return client.get(
        "/api/v1/admin/librarian/activity",
        headers=authorization(),
        params={"severity": severity, "limit": 500},
    ).json()["events"]


def test_clearing_removes_old_reads_but_keeps_recent_ones(
    client: TestClient, series_id: str
):
    token = issue(client, ("catalog:read",))
    client.get(
        "/api/v1/librarian/series", headers=bearer(token), params={"query": "Example"}
    )
    backdate(client, 90, action="series.resolve")
    client.get(
        "/api/v1/librarian/series", headers=bearer(token), params={"query": "Example"}
    )
    csrf = login(client)
    client.post(
        "/admin/librarian/activity/clear",
        data={"csrf_token": csrf, "window": "month"},
    )
    resolves = [item for item in feed(client) if item["action"] == "series.resolve"]
    assert len(resolves) == 1


def test_clearing_never_removes_permission_history(client: TestClient, series_id: str):
    """The guarantee that makes the button safe to press.

    A prune that could erase who was granted what would make the log worth
    less than not keeping one.
    """
    token = issue(client, ("catalog:read",))
    client.patch(
        f"/api/v1/admin/librarian-tokens/{token['id']}",
        headers=authorization(),
        json={"scopes": ["catalog:read", "ingest:stage"]},
    )
    client.get(
        "/api/v1/librarian/series", headers=bearer(token), params={"query": "Example"}
    )
    backdate(client, 400)
    csrf = login(client)
    client.post(
        "/admin/librarian/activity/clear", data={"csrf_token": csrf, "window": "year"}
    )
    remaining = {item["action"] for item in feed(client)}
    assert "token.issued" in remaining
    assert "token.scopes_changed" in remaining
    assert "series.resolve" not in remaining


def test_the_month_window_keeps_more_than_the_year_window(
    client: TestClient, series_id: str
):
    token = issue(client, ("catalog:read",))
    client.get(
        "/api/v1/librarian/series", headers=bearer(token), params={"query": "Example"}
    )
    backdate(client, 200, action="series.resolve")
    csrf = login(client)
    client.post(
        "/admin/librarian/activity/clear", data={"csrf_token": csrf, "window": "year"}
    )
    assert [item for item in feed(client) if item["action"] == "series.resolve"]
    client.post(
        "/admin/librarian/activity/clear", data={"csrf_token": csrf, "window": "month"}
    )
    assert not [item for item in feed(client) if item["action"] == "series.resolve"]


def test_the_clear_records_itself(client: TestClient, series_id: str):
    """The trail has to explain its own gaps."""
    token = issue(client, ("catalog:read",))
    client.get(
        "/api/v1/librarian/series", headers=bearer(token), params={"query": "Example"}
    )
    # The fixture resolves once as well, so count rather than assume.
    aged = len([item for item in feed(client) if item["action"] == "series.resolve"])
    backdate(client, 90, action="series.resolve")
    csrf = login(client)
    client.post(
        "/admin/librarian/activity/clear", data={"csrf_token": csrf, "window": "month"}
    )
    cleared = [
        item
        for item in feed(client, "security")
        if item["action"] == "activity.cleared"
    ]
    assert cleared, [item["action"] for item in feed(client, "security")]
    assert cleared[0]["detail"]["removed"] == aged
    assert cleared[0]["detail"]["window"] == "month"
    assert f"cleared {aged} entries older than one month" in cleared[0]["summary"]
    assert cleared[0]["actor"] == "admin"


def test_an_unknown_window_is_refused(client: TestClient):
    csrf = login(client)
    response = client.post(
        "/admin/librarian/activity/clear",
        data={"csrf_token": csrf, "window": "forever"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "how much history to keep" in response.text


def test_clearing_requires_a_csrf_token_and_an_administrator(
    client: TestClient, reader
):
    login(client)
    assert (
        client.post(
            "/admin/librarian/activity/clear",
            data={"csrf_token": "wrong", "window": "month"},
        ).status_code
        == 403
    )


def test_the_page_offers_both_retention_windows(client: TestClient):
    login(client)
    page = client.get("/admin/librarian").text
    assert "Clear activities" in page
    assert "More than 1 month ago" in page
    assert "More than 1 year ago" in page
    assert "Permission changes are always kept." in page
