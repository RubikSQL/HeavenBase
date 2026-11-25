from __future__ import annotations

from pathlib import Path

import pytest

from heavenbase.utils import file_uri_to_path, normalize_file_uri, path_to_file_uri


def test_local_file_uri_round_trips_reserved_and_unicode_paths(tmp_path):
    path = str(tmp_path / "space # percent% query? 雪.db")
    uri = path_to_file_uri(path)

    assert uri.startswith("file:///")
    assert "%23" in uri and "%25" in uri and "%3F" in uri
    assert Path(file_uri_to_path(uri)) == Path(path)  # Windows drive paths decode with forward slashes
    assert path_to_file_uri(file_uri_to_path(uri)) == uri


def test_local_file_uri_decodes_exactly_once(tmp_path):
    uri = path_to_file_uri(str(tmp_path / "%23.db"))

    assert "%2523.db" in uri
    assert path_to_file_uri(file_uri_to_path(uri)) == uri


def test_local_file_aliases_expand_to_canonical_uri(tmp_path):
    home = tmp_path / "home"
    resource = tmp_path / "resource"

    assert normalize_file_uri("file://%/db/main.sqlite", home=home) == path_to_file_uri(home / "db/main.sqlite")
    assert normalize_file_uri("file://&/fixtures/base.db", resource=resource) == path_to_file_uri(resource / "fixtures/base.db")


def test_windows_drive_uri_is_host_independent():
    uri = path_to_file_uri(r"c:\Users\A B\data#1.db")

    assert uri == "file:///C:/Users/A%20B/data%231.db"
    assert file_uri_to_path(uri) == "C:/Users/A B/data#1.db"


@pytest.mark.parametrize(
    "value",
    [
        "file:/tmp/legacy.db",
        "file:C:/legacy.db",
        "file:%/legacy.db",
        "file:&/legacy.db",
        "file:logical",
        "file://server/share.db",
        "file://user:password@server/share.db",
        "file:///tmp/a.db?mode=rw",
        "file:///tmp/a.db#fragment",
        "file:///tmp/%ZZ.db",
        "file:///tmp/a\\b.db",
        "file:///tmp/a%5Cb.db",
        "file:///tmp/%00.db",
    ],
)
def test_local_file_uri_rejects_legacy_network_and_decorated_forms(value):
    with pytest.raises(ValueError):
        file_uri_to_path(value)


def test_local_file_aliases_reject_root_escape_and_missing_roots(tmp_path):
    with pytest.raises(ValueError, match="package home"):
        file_uri_to_path("file://%/main.db")
    with pytest.raises(ValueError, match="escapes"):
        file_uri_to_path("file://%/../main.db", home=tmp_path)


def test_local_file_uri_encoder_rejects_nul():
    with pytest.raises(ValueError):
        path_to_file_uri("/tmp/a\x00b.db")
