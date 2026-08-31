"""freeweight.external.datasets — pinned datasets: downloaded, verified, extracted, or refused.

An unpinned dataset invalidates every comparison made against it, so verification is not a step
that can be skipped or deferred: a file is hashed **before** it is moved into place, a mismatch
refuses and names both hashes, and every result later carries the hash it ran against through
the manifest's ``dataset_hashes`` (which the reproducibility fingerprint already includes).

Archive extraction is hardened per Security Standards §5: no absolute paths, no ``..``
components, no symlinks or hardlinks, no device files, per-entry and total size caps, an
entry-count cap and a decompression-ratio cap. Extraction happens into a fresh temporary
directory inside the target's parent and is validated before anything is moved.
"""

from __future__ import annotations

import hashlib
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from freeweight.external.errors import DatasetHashMismatch, DatasetMissing, ExternalBenchmarkFailed

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "DatasetSpec",
    "extract_archive",
    "install_dataset",
    "sha256_file",
    "verify_dataset",
]

_CHUNK_BYTES = 1 << 20
_MAX_ENTRIES = 100_000
_MAX_ENTRY_BYTES = 1 << 31  # 2 GiB per entry
_MAX_RATIO = 200  # a zip bomb announces itself well before 200×


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    """One dataset a benchmark manifest pins.

    Attributes:
        name: The manifest's key for this dataset (``"input_data"``).
        url: Where it is fetched from. HTTPS in every shipped manifest.
        sha256: The pinned hash, ``sha256:``-prefixed. Never optional: a dataset without a pin
            cannot be installed by this module at all.
        filename: The name it is stored under inside the benchmark's ``datasets/`` directory.
        archive: Whether the download is an archive to extract (``.zip`` / ``.tar.gz``). The
            **archive file's** hash is what is pinned; extraction happens after verification.
    """

    name: str
    url: str
    sha256: str
    filename: str
    archive: bool = False


def sha256_file(path: Path) -> str:
    """Hash a file's content, ``sha256:``-prefixed, streaming so size does not matter."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def verify_dataset(path: Path, expected_sha256: str, *, name: str) -> None:
    """Prove an installed dataset still matches its pin, or refuse naming both hashes.

    Args:
        path: The installed file.
        expected_sha256: The manifest's pinned hash.
        name: The dataset's manifest key, for the refusal message.

    Raises:
        DatasetMissing: ``path`` does not exist — the remedy is ``freeweight external install``.
        DatasetHashMismatch: The content does not hash to the pin. ``details`` carries
            ``expected_sha256`` and ``actual_sha256`` — a refusal naming only one hash cannot be
            diagnosed.
    """
    if not path.exists():
        raise DatasetMissing(
            f"Dataset {name!r} is not installed at {path.name!r}. "
            "Run `freeweight external install` for this benchmark.",
            details={"dataset": name, "expected_path": str(path)},
        )
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise DatasetHashMismatch(
            f"Dataset {name!r} does not match its pinned hash: expected {expected_sha256}, "
            f"got {actual}. The file changed since it was pinned — a result measured against "
            "it would not be comparable to any other. Re-install, or update the manifest "
            "deliberately (which separates results).",
            details={
                "dataset": name,
                "expected_sha256": expected_sha256,
                "actual_sha256": actual,
            },
        )


def _download(url: str, destination: Path, *, cap_bytes: int, timeout_seconds: float) -> None:
    """Stream ``url`` into ``destination``, refusing past ``cap_bytes`` while streaming."""
    if not url.startswith(("https://", "http://")):
        raise ExternalBenchmarkFailed(
            f"Refusing to download from a non-HTTP(S) URL: {url!r}.", details={"url": url}
        )
    request = urllib.request.Request(url, headers={"User-Agent": "freeweight-external"})  # noqa: S310 — scheme checked above
    received = 0
    with (
        urllib.request.urlopen(request, timeout=timeout_seconds) as response,  # noqa: S310 — scheme checked above
        destination.open("wb") as out,
    ):
        while chunk := response.read(_CHUNK_BYTES):
            received += len(chunk)
            if received > cap_bytes:
                raise ExternalBenchmarkFailed(
                    f"Download exceeded the {cap_bytes}-byte cap and was abandoned.",
                    details={"url": url, "cap_bytes": str(cap_bytes)},
                )
            out.write(chunk)


def install_dataset(
    spec: DatasetSpec,
    datasets_dir: Path,
    *,
    cap_bytes: int,
    timeout_seconds: float,
    download: Callable[..., None] | None = None,
) -> Path:
    """Fetch one pinned dataset, verify it against its pin, then move it into place.

    Verification happens on the temporary download, **before** the move: a file that fails its
    pin never appears in ``datasets_dir`` at all, so a later run cannot find and trust it.
    Archives are extracted (hardened) after the archive file itself verified.

    Args:
        spec: What to fetch and the hash it must have.
        datasets_dir: The benchmark's ``datasets/`` directory.
        cap_bytes: Streaming size cap for the download.
        timeout_seconds: Network budget for the fetch.
        download: The fetch function; injected so tests never touch the network. The default
            streams over HTTPS with the cap enforced mid-stream.

    Returns:
        The installed path.

    Raises:
        DatasetHashMismatch: The download does not hash to the pin; nothing was installed.
        ExternalBenchmarkFailed: The download failed, exceeded the cap, or the archive failed a
            hardening check.
    """
    datasets_dir.mkdir(parents=True, exist_ok=True)
    fetch = download if download is not None else _download
    with tempfile.TemporaryDirectory(dir=datasets_dir) as staging_name:
        staging = Path(staging_name)
        candidate = staging / spec.filename
        fetch(spec.url, candidate, cap_bytes=cap_bytes, timeout_seconds=timeout_seconds)
        actual = sha256_file(candidate)
        if actual != spec.sha256:
            raise DatasetHashMismatch(
                f"Downloaded dataset {spec.name!r} does not match its pinned hash: expected "
                f"{spec.sha256}, got {actual}. Nothing was installed.",
                details={
                    "dataset": spec.name,
                    "expected_sha256": spec.sha256,
                    "actual_sha256": actual,
                },
            )
        target = datasets_dir / spec.filename
        if spec.archive:
            extracted = staging / "extracted"
            extract_archive(candidate, extracted)
            final = datasets_dir / spec.name
            if final.exists():
                shutil.rmtree(final)
            shutil.move(str(extracted), str(final))
            shutil.move(str(candidate), str(target))
            return final
        target.unlink(missing_ok=True)
        shutil.move(str(candidate), str(target))
        return target


def _check_member(name: str, size: int, *, is_link: bool, is_special: bool, entries: int) -> None:
    """One entry's hardening checks; raises on the first violation."""
    path = Path(name)
    if path.is_absolute():
        raise ExternalBenchmarkFailed(
            f"Archive entry has an absolute path: {name!r}.", details={"entry": name}
        )
    if ".." in path.parts:
        raise ExternalBenchmarkFailed(
            f"Archive entry escapes its directory: {name!r}.", details={"entry": name}
        )
    if is_link:
        raise ExternalBenchmarkFailed(
            f"Archive entry is a link: {name!r}; links are refused.", details={"entry": name}
        )
    if is_special:
        raise ExternalBenchmarkFailed(
            f"Archive entry is a device or special file: {name!r}.", details={"entry": name}
        )
    if size > _MAX_ENTRY_BYTES:
        raise ExternalBenchmarkFailed(
            f"Archive entry {name!r} exceeds the per-entry size cap.", details={"entry": name}
        )
    if entries > _MAX_ENTRIES:
        raise ExternalBenchmarkFailed(
            "Archive exceeds the entry-count cap.", details={"entries": str(entries)}
        )


def extract_archive(archive: Path, destination: Path) -> None:
    """Extract a ``.zip`` or ``.tar.gz`` archive with every hardening check applied first.

    All checks run over the member list before a single byte is extracted, and extraction goes
    into ``destination`` (created fresh) — never over existing files.

    Args:
        archive: The verified archive file.
        destination: The directory to create and fill.

    Raises:
        ExternalBenchmarkFailed: An absolute path, a ``..`` component, a link, a device file, an
            oversize entry, too many entries, or a decompression ratio past the zip-bomb cap.
    """
    destination.mkdir(parents=True, exist_ok=False)
    archive_bytes = max(archive.stat().st_size, 1)
    total = 0
    entries = 0
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                entries += 1
                # Zip stores symlinks in the external attributes' mode bits.
                mode = (info.external_attr >> 16) & 0o170000
                _check_member(
                    info.filename,
                    info.file_size,
                    is_link=mode == 0o120000,
                    is_special=mode not in (0, 0o100000, 0o040000, 0o120000),
                    entries=entries,
                )
                total += info.file_size
                if total > archive_bytes * _MAX_RATIO:
                    raise ExternalBenchmarkFailed(
                        "Archive decompresses past the ratio cap (zip-bomb guard).",
                        details={"archive": archive.name},
                    )
            bundle.extractall(destination)  # noqa: S202 — every member vetted above
        return
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as bundle:
            members = bundle.getmembers()
            for member in members:
                entries += 1
                _check_member(
                    member.name,
                    member.size,
                    is_link=member.issym() or member.islnk(),
                    is_special=member.isdev() or member.isfifo(),
                    entries=entries,
                )
                total += member.size
                if total > archive_bytes * _MAX_RATIO:
                    raise ExternalBenchmarkFailed(
                        "Archive decompresses past the ratio cap (zip-bomb guard).",
                        details={"archive": archive.name},
                    )
            bundle.extractall(destination, members=members, filter="data")
        return
    raise ExternalBenchmarkFailed(
        f"{archive.name!r} is neither a zip nor a tar archive.", details={"archive": archive.name}
    )
