"""freeweight.services.providers — reading and editing the ``[provider]`` block in the config file.

[ADR-0117](../../docs/adr/0117-provider-registrations-are-edited-in-place-in-the-config-file.md):
``config.toml`` stays the single source of truth and the Providers page edits it *in place*, so
an operator's comments, key order and formatting survive a write. FreeWeight has one provider, not
a registry (spec §12), so there is one block to edit and nothing to name.

Nothing here writes any other part of the file. The candidate document is loaded through
:func:`~freeweight.config.load_settings` before it lands, the previous file is kept as
``config.toml.bak``, and a file that changed under the editor is refused rather than overwritten.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final

import tomlkit
from baseaicore import SuiteError, ValidationError
from pydantic import ValidationError as PydanticValidationError

from freeweight.config import ENV_PREFIX, ProviderSettings, load_settings

if TYPE_CHECKING:
    from tomlkit import TOMLDocument

    from freeweight.config import Settings

__all__ = [
    "WRITABLE_FIELDS",
    "close_provider",
    "ProviderConfigChanged",
    "ProviderView",
    "config_digest",
    "describe_provider",
    "save_provider",
]

WRITABLE_FIELDS: Final[tuple[str, ...]] = (
    "kind",
    "base_url",
    "timeout_seconds",
    "model_directory",
    "state_dir",
    "server_path",
)
"""Every field of :class:`~freeweight.config.ProviderSettings`, which is the whole block."""


def close_provider(provider: object) -> None:
    """Release a provider handle, and log rather than raise if it will not let go.

    A supervising provider owns an operating-system process: ``LlamaCppProvider`` terminates its
    server in ``close()`` and otherwise only in a finalizer, so a handle that is merely dropped
    leaves a ``llama-server`` holding the card until the interpreter exits — and the next run is
    then refused for want of VRAM, which reads as a defect in measurement rather than in cleanup.
    Called when a provider edit replaces the handle, and when the server stops.

    Args:
        provider: The handle to release. A provider with no ``close`` is left alone.
    """
    close = getattr(provider, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:  # noqa: BLE001 — a failed release must not propagate into a page
        logging.getLogger(__name__).warning("provider.close_failed", exc_info=True)


class ProviderConfigChanged(SuiteError):
    """The file changed since it was read; the write is refused rather than applied blind."""

    code: ClassVar[str] = "CONFLICT"


@dataclass(frozen=True, slots=True)
class ProviderView:
    """The configured provider as the page renders it."""

    kind: str
    base_url: str
    timeout_seconds: float
    model_directory: str
    state_dir: str
    server_path: str
    shadowed_by: str

    def as_json(self) -> dict[str, Any]:
        """The block as JSON, with ``shadowed_by`` empty when nothing shadows it."""
        return {
            "kind": self.kind,
            "base_url": self.base_url,
            "timeout_seconds": self.timeout_seconds,
            "model_directory": self.model_directory,
            "state_dir": self.state_dir,
            "server_path": self.server_path,
            "shadowed_by": self.shadowed_by,
        }


def describe_provider(settings: Settings) -> ProviderView:
    """The configured provider, and the environment variable that pins it if one does.

    The environment sits above the file (configuration standards §7), so a variable setting any
    ``[provider]`` key makes an edit to that key inert. The page says so rather than letting an
    operator watch a saved value do nothing.
    """
    prefix = f"{ENV_PREFIX}PROVIDER__"
    shadowed_by = next((key for key in sorted(os.environ) if key.startswith(prefix)), "")
    provider = settings.provider
    return ProviderView(
        kind=provider.kind,
        base_url=provider.base_url,
        timeout_seconds=provider.timeout_seconds,
        model_directory=provider.model_directory,
        state_dir=provider.state_dir,
        server_path=provider.server_path,
        shadowed_by=shadowed_by,
    )


def config_digest(config_path: Path) -> str:
    """The digest of the file as it stands, or ``""`` when there is no file yet."""
    if not config_path.exists():
        return ""
    return hashlib.sha256(config_path.read_bytes()).hexdigest()


def save_provider(
    config_path: Path, values: dict[str, Any], *, base_digest: str | None = None
) -> None:
    """Write the ``[provider]`` block, leaving the rest of the file exactly as it was.

    Args:
        config_path: The configuration file to edit.
        values: Any of :data:`WRITABLE_FIELDS`. An absent key keeps its current value; an empty
            string for an optional key removes it from the file.
        base_digest: The digest the form was rendered from, if the caller took one.

    Raises:
        ValidationError: ``values`` names a key outside the provider block, or a value the
            provider model rejects. Nothing is written.
        ProviderConfigChanged: The file changed since ``base_digest`` was taken.
        SuiteError: The resulting document is not a configuration this application would load.
    """
    refused = sorted(set(values) - set(WRITABLE_FIELDS))
    if refused:
        message = (
            f"{', '.join(refused)} cannot be written here. The provider block's writable keys are "
            f"{', '.join(WRITABLE_FIELDS)}; everything else in the file stays config-only."
        )
        raise ValidationError(message, details={"refused": refused})
    try:
        ProviderSettings.model_validate({**values})
    except PydanticValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or "provider"
        message = f"{field}: {first['msg']}"
        raise ValidationError(message, details={"field": field}) from exc
    if base_digest is not None and base_digest != config_digest(config_path):
        message = (
            f"{config_path} changed since this page was loaded; the write was refused so that an "
            "edit made elsewhere is not overwritten. Reload the page and apply the change again."
        )
        raise ProviderConfigChanged(message, details={"file": str(config_path)})

    document: TOMLDocument = (
        tomlkit.parse(config_path.read_text(encoding="utf-8"))
        if config_path.exists()
        else tomlkit.document()
    )
    if "provider" not in document:
        document["provider"] = tomlkit.table()
    block = document["provider"]
    for field_name in WRITABLE_FIELDS:
        if field_name not in values:
            continue
        value = values[field_name]
        if value == "" and field_name != "kind":
            block.pop(field_name, None)
            continue
        block[field_name] = value

    config_path.parent.mkdir(parents=True, exist_ok=True)
    candidate = config_path.with_name(config_path.name + ".new")
    candidate.write_text(tomlkit.dumps(document), encoding="utf-8")
    try:
        load_settings(config_path=candidate)
    except Exception:
        candidate.unlink(missing_ok=True)
        raise
    if config_path.exists():
        config_path.replace(config_path.with_name(config_path.name + ".bak"))
    candidate.replace(config_path)
