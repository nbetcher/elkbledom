"""Protect flat, reproducible release contents and existing artifacts."""

from __future__ import annotations

from zipfile import ZipFile

import pytest

from scripts import build_release


def test_release_is_flat_reproducible_complete_and_never_overwrites(tmp_path, monkeypatch) -> None:
    component = tmp_path / "component"
    component.mkdir()
    (component / "manifest.json").write_text('{"version":"2.0.17"}', encoding="utf-8")
    (component / "transport.py").write_bytes(b"# transport\n")
    (component / "translations").mkdir()
    (component / "translations" / "en.json").write_bytes(b"{}\n")
    (component / "__pycache__").mkdir()
    (component / "__pycache__" / "transport.pyc").write_bytes(b"not for release")
    monkeypatch.setattr(build_release, "COMPONENT", component)
    first = build_release.build_release(tmp_path / "first.zip")
    second = build_release.build_release(tmp_path / "second.zip")
    original = first.read_bytes()
    assert second.read_bytes() == original
    with ZipFile(first) as archive:
        assert set(archive.namelist()) == {"manifest.json", "transport.py", "translations/en.json"}
        assert archive.read("transport.py") == b"# transport\n"
    with pytest.raises(FileExistsError):
        build_release.build_release(first)
    assert first.read_bytes() == original
