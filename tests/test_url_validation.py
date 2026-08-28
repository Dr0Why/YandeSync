import unicodedata

import pytest

from yande_sync.security import (
    SecurityError,
    safe_child,
    safe_folder_name,
    validate_file_extension,
    validate_file_metadata,
    validate_tag_query,
    validate_url,
)


@pytest.mark.parametrize("url", [
    "http://yande.re/post.json",
    "https://example.com/post.json",
    "https://yande.re.evil.example/post.json",
    "https://files.yande.re.example.com/x.jpg",
    "https://evil.example/?url=yande.re",
    "https://yande.re:444/post.json",
    "https://user:pass@yande.re/post.json",
])
def test_rejects_unsafe_urls(url):
    with pytest.raises(SecurityError):
        validate_url(url)


def test_exact_endpoints_only():
    assert validate_url("https://yande.re/post.json", purpose="api")
    assert validate_url("https://files.yande.re/image/a.jpg", purpose="file")
    with pytest.raises(SecurityError):
        validate_url("https://yande.re/tag.json", purpose="api")
    with pytest.raises(SecurityError):
        validate_url("https://yande.re/post.json", purpose="file")


def test_removed_metadata_purpose_is_rejected():
    with pytest.raises(SecurityError, match="unsupported URL purpose"):
        validate_url("https://yande.re/tag.json?name=artist", purpose="metadata")


def test_safe_folder_and_child(tmp_path):
    folder = safe_folder_name("../artist:name")
    assert folder.startswith(".._artist_name--")
    assert safe_child(tmp_path, "123.jpg").parent == tmp_path.resolve()
    with pytest.raises(SecurityError):
        safe_child(tmp_path, "../escape.jpg")


@pytest.mark.parametrize("extension", ["jpg:secret", "../jpg", "exe", "jpg.part", ""])
def test_rejects_unsafe_or_unsupported_extensions(extension):
    with pytest.raises(SecurityError):
        validate_file_extension(extension)


def test_rejects_reserved_folder_and_terminal_controls():
    assert safe_folder_name("CON").startswith("query_CON--")
    with pytest.raises(SecurityError):
        validate_tag_query("tag\x1b[2J")


@pytest.mark.parametrize(
    "query",
    ["rating:safe", "width:>=1920", "CON", "name.", "name ", "..", "a/b", "a\\b"],
)
def test_unsafe_query_text_gets_stable_collision_suffix(query):
    folder = safe_folder_name(query)
    assert "--" in folder
    assert folder == safe_folder_name(query)
    assert not folder.endswith((" ", "."))


def test_case_unicode_replacement_and_truncation_collisions_are_disambiguated():
    assert safe_folder_name("TAG", existing_names=["tag"]) != "TAG"
    composed = "é_artist"
    decomposed = unicodedata.normalize("NFD", composed)
    first = safe_folder_name(composed)
    second = safe_folder_name(decomposed, existing_names=[first])
    assert first.casefold() != second.casefold()
    assert safe_folder_name("a:b") != safe_folder_name("a?b")
    truncated = safe_folder_name("x" * 500)
    assert len(truncated) <= 120 and "--" in truncated


@pytest.mark.parametrize(("size", "md5"), [(0, "a" * 32), (1, "x" * 32), (-1, "0" * 32)])
def test_rejects_invalid_remote_file_metadata(size, md5):
    with pytest.raises(SecurityError):
        validate_file_metadata(size, md5)
