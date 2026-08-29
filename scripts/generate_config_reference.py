"""Generate ``docs/configuration.md`` from the settings model, so the reference cannot drift.

Configuration Standards §8: each application publishes a configuration reference *generated from
the settings model*, listing per field its key path, environment variable, type, default, valid
range, whether it is runtime-changeable, its security implications and an example — and a CI test
fails when the committed document differs from the generated one.

Everything in the table comes from :class:`freeweight.config.Settings`: the type and the range
from each field's annotation and constraints, the description and the example from the field's
own ``description`` and ``examples``, runtime-changeability from
:data:`freeweight.services.settings.RUNTIME_SETTINGS` and the security column from
:data:`freeweight.services.settings.CONFIG_ONLY_KEYS`. A field added to the model without a
description fails generation rather than producing an empty cell, because an undocumented key is
the drift this document exists to prevent.

Usage:
    python scripts/generate_config_reference.py            # write docs/configuration.md
    python scripts/generate_config_reference.py --check    # exit 1 if the committed file is stale
"""

from __future__ import annotations

import argparse
import sys
import types
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET = REPO_ROOT / "docs" / "configuration.md"

sys.path.insert(0, str(REPO_ROOT / "src"))

from pydantic import BaseModel  # noqa: E402
from pydantic_core import PydanticUndefined  # noqa: E402

from freeweight.config import ENV_PREFIX, Settings  # noqa: E402
from freeweight.services.settings import CONFIG_ONLY_KEYS, RUNTIME_SETTINGS  # noqa: E402

_RUNTIME_KEYS = frozenset(setting.key for setting in RUNTIME_SETTINGS)

_SECURITY_NOTES: dict[str, str] = {
    "auth.tokens": (
        "Secret. Redacted by `config show`, never logged; required for a non-loopback bind."
    ),
    "server.host": (
        "Config only. A non-loopback bind exposes the service beyond this machine (ADR-0026)."
    ),
    "server.allow_lan_exposure": (
        "Config only. The acknowledgement that makes a `0.0.0.0` bind deliberate."
    ),
    "server.allowed_hosts": "Config only. Defends a non-loopback bind against DNS rebinding.",
    "providers.allow_remote": (
        "Config only. Lets content leave this machine; one half of the remote opt-in."
    ),
    "judge.allow_remote": (
        "Config only. Lets a candidate's output leave this machine to be judged."
    ),
    "logging.include_content": (
        "Config only. Logs full prompts and responses when on; hashes only when off."
    ),
    "storage.database_url": (
        "Config only. May carry a PostgreSQL password; redacted by `config show`."
    ),
    "storage.artifact_dir": "Config only. Where raw responses and generated code are written.",
    "goals.root": "Config only. Where hand-editable goal packs are read from.",
    "provider.base_url": "Config only. Where prompts are sent.",
    "server.port": "Config only. Part of the bind.",
}


def _toml(value: Any) -> str:  # noqa: ANN401 — any example value
    """Render a value as a TOML literal."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _type_name(annotation: Any) -> str:  # noqa: ANN401 — a type annotation
    """Render an annotation the way a reader of TOML thinks of it."""
    origin = get_origin(annotation)
    if origin is Literal:
        return "one of " + ", ".join(f"`{_toml(item)}`" for item in get_args(annotation))
    if origin in (types.UnionType,) or str(origin) == "typing.Union":
        members = [arg for arg in get_args(annotation) if arg is not type(None)]
        optional = len(members) < len(get_args(annotation))
        inner = " or ".join(_type_name(member) for member in members)
        return f"{inner}, optional" if optional else inner
    if origin is tuple:
        (item, *_rest) = get_args(annotation) or (str,)
        return f"list of {_type_name(item)}"
    if annotation is str:
        return "string"
    if annotation is int:
        return "integer"
    if annotation is float:
        return "number"
    if annotation is bool:
        return "boolean"
    return getattr(annotation, "__name__", str(annotation))


def _default(field: Any) -> str:  # noqa: ANN401 — a pydantic FieldInfo
    """Render the default: a literal, `unset`, or `derived` for a factory."""
    if field.default_factory is not None:
        return "derived"
    if field.default is PydanticUndefined:
        return "required"
    if field.default is None:
        return "unset"
    if isinstance(field.default, tuple) and not field.default:
        return "`[]`"
    return f"`{_toml(field.default)}`"


def _range(field: Any, annotation: Any) -> str:  # noqa: ANN401 — a FieldInfo and its annotation
    """Render the constraints as a reader would state them."""
    parts: list[str] = []
    for constraint in field.metadata:
        for name, symbol in (("ge", "≥"), ("gt", ">"), ("le", "≤"), ("lt", "<")):
            value = getattr(constraint, name, None)
            if value is not None:
                parts.append(f"{symbol} {_toml(value)}")
    if get_origin(annotation) is Literal:
        return "listed values"
    return ", ".join(parts) if parts else "—"


def _security(key: str) -> str:
    """The security column: the config-only rule, and the specific risk where there is one."""
    if key in _SECURITY_NOTES:
        return _SECURITY_NOTES[key]
    if key in CONFIG_ONLY_KEYS:
        return "Config only: refused by the settings API and the UI."
    return "—"


def _runtime(key: str) -> str:
    """Whether the settings page and `PUT /api/v1/settings` may change it."""
    if key in _RUNTIME_KEYS:
        return "yes — applies to work started from now on"
    return "no — file or environment, then restart"


def _rows(section_name: str, model: type[BaseModel]) -> list[str]:
    """One table row per field of one section."""
    rows: list[str] = []
    for field_name, field in model.model_fields.items():
        key = f"{section_name}.{field_name}"
        if not field.description:
            raise SystemExit(f"{key} has no description; add one to the settings model.")
        if not field.examples:
            raise SystemExit(f"{key} has no example; add one to the settings model.")
        env = f"`{ENV_PREFIX}{section_name.upper()}__{field_name.upper()}`"
        description = field.description.replace("|", "\\|")
        rows.append(
            f"| `{key}` | {env} | {_type_name(field.annotation)} | {_default(field)} | "
            f"{_range(field, field.annotation)} | {_runtime(key)} | {_security(key)} | "
            f"`{_toml(field.examples[0])}` | {description} |"
        )
    return rows


def render() -> str:
    """Render the whole document."""
    lines = [
        "# FreeWeight configuration reference",
        "",
        "**Generated from `freeweight.config.Settings` by",
        "`scripts/generate_config_reference.py`.**",
        "Do not edit by hand: CI fails when this file differs from what the model generates.",
        "",
        "Precedence, lowest to highest: built-in defaults → `config.toml` → `FREEWEIGHT_*`",
        "environment variables → CLI flags (configuration standards §1). Overriding is per leaf",
        "field, never per section. Database-backed runtime settings sit between the file and the",
        "environment (§7). The file lives at `$XDG_CONFIG_HOME/freeweight/config.toml`;",
        "`freeweight config path` prints the resolved location and `freeweight config init` writes",
        "a commented example.",
        "",
        "Environment variables spell the key path as `FREEWEIGHT_<SECTION>__<FIELD>`; a list is",
        "comma-separated. Two conveniences exist outside that scheme: `FREEWEIGHT_CONFIG` names",
        "the file and `FREEWEIGHT_DATA_DIR` moves the data directory.",
        "",
        "**Runtime-changeable** means the settings page and `PUT /api/v1/settings` may change it",
        "and the change applies to work started from then on. Everything else is read once at",
        "startup. **Config only** keys decide who can reach this machine, what leaves it, or where",
        "its data lives; the settings API refuses them with `FORBIDDEN` (configuration standards",
        "§7).",
        "",
    ]
    for section_name, section_field in Settings.model_fields.items():
        model = section_field.annotation
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            continue
        doc = (model.__doc__ or "").strip().splitlines()
        lines.append(f"## `[{section_name}]`")
        lines.append("")
        if doc:
            lines.append(doc[0].strip())
            lines.append("")
        lines.append(
            "| Key | Environment variable | Type | Default | Valid range | Runtime-changeable | "
            "Security | Example | Meaning |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|")
        lines.extend(_rows(section_name, model))
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def main() -> int:
    """Write or verify the reference.

    Returns:
        ``0`` when written, or current under ``--check``; ``1`` when ``--check`` finds it stale.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify rather than write.")
    args = parser.parse_args()
    intended = render()
    current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else None
    if args.check:
        if current != intended:
            print(f"stale: {TARGET.relative_to(REPO_ROOT)}")
            print("Run: python scripts/generate_config_reference.py")
            return 1
        print(f"current: {TARGET.relative_to(REPO_ROOT)}")
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(intended, encoding="utf-8")
    state = "changed" if current != intended else "unchanged"
    print(f"Wrote {TARGET.relative_to(REPO_ROOT)} ({state}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
