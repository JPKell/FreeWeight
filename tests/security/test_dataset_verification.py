"""Datasets are pinned, verified before use, and archives cannot escape their directory.

The hash-mismatch refusal is mutation-checked in the test that reads both hashes out of the
error: comparing against the wrong pin must refuse and name both values, because a comparison
made against an unpinned dataset is meaningless.
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from freeweight.external.datasets import (
    DatasetSpec,
    extract_archive,
    install_dataset,
    is_placeholder_pin,
    sha256_file,
    verify_dataset,
)
from freeweight.external.errors import DatasetHashMismatch, DatasetMissing, ExternalBenchmarkFailed


def _write(path: Path, data: bytes) -> str:
    path.write_bytes(data)
    return sha256_file(path)


class TestVerification:
    def test_a_matching_dataset_passes(self, tmp_path: Path) -> None:
        data = tmp_path / "d.jsonl"
        digest = _write(data, b'{"q": 1}\n')

        verify_dataset(data, digest, name="d")  # does not raise

    def test_a_missing_dataset_names_the_install_command(self, tmp_path: Path) -> None:
        with pytest.raises(DatasetMissing) as excinfo:
            verify_dataset(tmp_path / "absent.jsonl", "sha256:" + "0" * 64, name="absent")

        assert excinfo.value.code == "DATASET_MISSING"
        assert "external install" in str(excinfo.value.message)

    def test_a_mismatch_refuses_and_names_both_hashes(self, tmp_path: Path) -> None:
        # A *varied* wrong pin: a repeated-character pin is recognized as a shipped
        # placeholder (M6-8) and takes the other branch, tested below.
        data = tmp_path / "d.jsonl"
        actual = _write(data, b"real content")
        wrong = "sha256:" + "ab" * 32

        with pytest.raises(DatasetHashMismatch) as excinfo:
            verify_dataset(data, wrong, name="d")

        assert excinfo.value.code == "DATASET_HASH_MISMATCH"
        assert excinfo.value.details["expected_sha256"] == wrong
        assert excinfo.value.details["actual_sha256"] == actual
        assert wrong in str(excinfo.value.message)
        assert actual in str(excinfo.value.message)
        assert "placeholder" not in str(excinfo.value.message)

    def test_a_placeholder_pin_mismatch_says_it_is_a_placeholder(self, tmp_path: Path) -> None:
        """M6-8: the shipped pins are placeholders, and the refusal must say so.

        A user who installs a real dataset against a shipped manifest hits this exact error;
        "the file changed since it was pinned" would send them chasing corruption that never
        happened, when the actual remedy is recording the true hash.
        """
        data = tmp_path / "d.jsonl"
        actual = _write(data, b"the real dataset")
        placeholder = "sha256:" + "0" * 64

        with pytest.raises(DatasetHashMismatch) as excinfo:
            verify_dataset(data, placeholder, name="d")

        assert excinfo.value.code == "DATASET_HASH_MISMATCH"
        assert excinfo.value.details["placeholder_pin"] is True
        assert "placeholder" in str(excinfo.value.message)
        assert actual in str(excinfo.value.message)

    def test_is_placeholder_pin_recognizes_only_the_shipped_shape(self) -> None:
        assert is_placeholder_pin("sha256:" + "0" * 64) is True
        assert is_placeholder_pin("sha256:" + "1" * 64) is True
        assert is_placeholder_pin("sha256:" + "ab" * 32) is False
        assert is_placeholder_pin("sha256:" + "0" * 63) is False
        assert is_placeholder_pin("md5:" + "0" * 64) is False
        assert is_placeholder_pin("0" * 64) is False


class TestInstall:
    def test_a_download_that_matches_is_installed(self, tmp_path: Path) -> None:
        payload = b'{"case": 1}\n'
        digest = f"sha256:{__import__('hashlib').sha256(payload).hexdigest()}"

        def fake_download(url: str, destination: Path, **_: object) -> None:
            destination.write_bytes(payload)

        installed = install_dataset(
            DatasetSpec(name="cases", url="https://x/y", sha256=digest, filename="cases.jsonl"),
            tmp_path,
            cap_bytes=1_000_000,
            timeout_seconds=10,
            download=fake_download,
        )

        assert installed.read_bytes() == payload

    def test_a_download_that_fails_its_pin_installs_nothing(self, tmp_path: Path) -> None:
        def fake_download(url: str, destination: Path, **_: object) -> None:
            destination.write_bytes(b"tampered")

        datasets_dir = tmp_path / "datasets"
        with pytest.raises(DatasetHashMismatch):
            install_dataset(
                DatasetSpec(
                    name="cases",
                    url="https://x/y",
                    sha256="sha256:" + "0" * 64,
                    filename="cases.jsonl",
                ),
                datasets_dir,
                cap_bytes=1_000_000,
                timeout_seconds=10,
                download=fake_download,
            )

        assert list(datasets_dir.iterdir()) == [], "a failed dataset must leave nothing behind"


class TestArchiveHardening:
    def _zip(self, tmp_path: Path, members: list[tuple[str, bytes]]) -> Path:
        archive = tmp_path / "a.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            for name, data in members:
                bundle.writestr(name, data)
        return archive

    def test_a_clean_zip_extracts(self, tmp_path: Path) -> None:
        archive = self._zip(tmp_path, [("data/a.txt", b"hello")])

        extract_archive(archive, tmp_path / "out")

        assert (tmp_path / "out" / "data" / "a.txt").read_bytes() == b"hello"

    def test_a_traversal_entry_is_refused(self, tmp_path: Path) -> None:
        archive = self._zip(tmp_path, [("../escape.txt", b"x")])

        with pytest.raises(ExternalBenchmarkFailed, match="escapes its directory"):
            extract_archive(archive, tmp_path / "out")

    def test_an_absolute_path_entry_is_refused(self, tmp_path: Path) -> None:
        archive = tmp_path / "abs.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            info = zipfile.ZipInfo("/etc/passwd")
            bundle.writestr(info, b"x")

        with pytest.raises(ExternalBenchmarkFailed, match="absolute path"):
            extract_archive(archive, tmp_path / "out")

    def test_a_symlink_entry_is_refused(self, tmp_path: Path) -> None:
        archive = tmp_path / "link.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            info = zipfile.ZipInfo("link")
            info.external_attr = (0o120000 | 0o777) << 16
            bundle.writestr(info, "/etc/passwd")

        with pytest.raises(ExternalBenchmarkFailed, match="is a link"):
            extract_archive(archive, tmp_path / "out")

    def test_a_tar_symlink_is_refused(self, tmp_path: Path) -> None:
        archive = tmp_path / "a.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            info = tarfile.TarInfo("evil")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            bundle.addfile(info)

        with pytest.raises(ExternalBenchmarkFailed, match="is a link"):
            extract_archive(archive, tmp_path / "out")

    def test_a_zip_bomb_ratio_is_refused(self, tmp_path: Path) -> None:
        archive = tmp_path / "bomb.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("big.bin", b"\0" * (5 * 1024 * 1024))

        with pytest.raises(ExternalBenchmarkFailed, match="ratio cap"):
            extract_archive(archive, tmp_path / "out")

    def test_a_clean_tar_extracts(self, tmp_path: Path) -> None:
        archive = tmp_path / "a.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            data = b"content"
            info = tarfile.TarInfo("dir/file.txt")
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))

        extract_archive(archive, tmp_path / "out")

        assert (tmp_path / "out" / "dir" / "file.txt").read_bytes() == b"content"
