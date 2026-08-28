"""freeweight.cli.commands.prompts — list, show, build.

The three commands prompt standards §3 names: two that read the installed pack, and one that
regenerates its manifest so a record edited without a rebuild fails CI rather than reaching a
benchmark's provenance.

Every command here is **local** mode (CLI standards §6): it reads the pack shipped inside the
installed package, opens no database and needs no server. ``build`` is the one command in this
group that writes, and it writes exactly one file — ``manifest.json`` beside the records it
describes.

Only ``typer`` and ``json`` are imported at module level, so registering this subgroup (which
:mod:`freeweight.cli.main` does eagerly, to build ``--help``) never pulls in Jinja2 (CLI standards
§12).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from freeweight.services.prompts import PromptLibrary, PromptRecord

__all__ = ["app"]

app = typer.Typer(help="Inspect and rebuild the prompt pack.")

_JsonOption = Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")]
_VersionOption = Annotated[
    str | None, typer.Option("--version", help="An exact record version; default is the latest.")
]


def _load() -> PromptLibrary:
    """Load the pack shipped inside the installed package, or exit 3.

    The *shipped* pack, with no override directory applied — deliberately the same call
    :func:`freeweight.services.runs.shipped_prompt_library` makes, so what this command prints is
    what a run would actually render. Override loading exists in the loader
    (:func:`~freeweight.services.prompts.load_pack`'s ``override_root``) and is wired to
    configuration at the phase that also teaches a run to refuse an overridden prompt without
    ``--allow-prompt-override`` (prompt standards §6); a CLI that displayed overrides before then
    would describe a pack no benchmark uses.

    Exit 3 rather than 1: a malformed pack is a configuration error in CLI standards §4's table
    ("invalid config, unsafe combination, missing prompt pack"), and it is the same exit code
    startup uses for the same condition.
    """
    from freeweight.services.prompts import PACK_ROOT, PromptPackInvalid, load_pack

    try:
        return load_pack(PACK_ROOT)
    except PromptPackInvalid as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc


def _record_json(record: PromptRecord) -> dict[str, object]:
    """Render one record as the object ``--json`` prints, matching the HTTP field names."""
    return {
        "prompt_id": record.prompt_id,
        "version": record.version,
        "sha256": record.sha256,
        "source": record.source,
        "purpose": record.purpose,
        "variables": {
            name: {
                "type": spec.type_name,
                "required": spec.required,
                "description": spec.description,
                "default": spec.default,
                "minimum": spec.minimum,
                "maximum": spec.maximum,
            }
            for name, spec in sorted(record.variables.items())
        },
    }


@app.command("list")
def list_prompts(json_output: _JsonOption = False) -> None:
    """List every prompt record in the installed pack. Mode: local.

    Example:
        freeweight prompts list --task goal.my_voice
    """
    library = _load()
    records = library.all_records()
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "pack_id": library.pack_id,
                    "pack_version": library.pack_version,
                    "pack_sha256": library.pack_hash(),
                    "prompts": [_record_json(record) for record in records],
                }
            )
        )
        return
    typer.echo(f"{library.pack_id} {library.pack_version}  {library.pack_hash()}")
    for record in records:
        marker = " (overridden)" if record.source == "user_override" else ""
        typer.echo(f"  {record.prompt_id}  v{record.version}{marker}  {record.purpose}")


@app.command("show")
def show(
    prompt_id: Annotated[
        str, typer.Argument(help="The dotted prompt id, e.g. benchmarks.agent.goal.")
    ],
    version: _VersionOption = None,
    json_output: _JsonOption = False,
) -> None:
    """Show one record: its purpose, variables, template and hash. Mode: local.

    Example:
        freeweight prompts show goals.judge.rubric
    """
    from freeweight.services.prompts import PromptNotFound

    library = _load()
    try:
        record = library.get(prompt_id, version=version)
    except PromptNotFound as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(2) from exc

    if json_output:
        body = _record_json(record)
        body["system"] = record.system
        body["template"] = record.template
        typer.echo(json.dumps(body))
        return

    typer.echo(f"{record.prompt_id}  v{record.version}")
    typer.echo(f"  source:  {record.source}")
    typer.echo(f"  sha256:  {record.sha256}")
    typer.echo(f"  purpose: {record.purpose}")
    typer.echo("  variables:")
    for name, spec in sorted(record.variables.items()):
        requiredness = "required" if spec.required else f"optional, default {spec.default!r}"
        typer.echo(f"    {name} ({spec.type_name}, {requiredness}) — {spec.description}")
    if record.system is not None:
        typer.echo("  system:")
        for line in record.system.splitlines():
            typer.echo(f"    {line}")
    typer.echo("  template:")
    for line in record.template.splitlines():
        typer.echo(f"    {line}")


@app.command("build")
def build(
    check: Annotated[
        bool,
        typer.Option("--check", help="Report drift and exit 5 without writing; for CI."),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print what would be written and change nothing.")
    ] = False,
    generated_at: Annotated[
        str | None,
        typer.Option("--generated-at", help="RFC 3339 stamp; default keeps the existing one."),
    ] = None,
    json_output: _JsonOption = False,
) -> None:
    """Regenerate ``manifest.json`` from the records on disk. Mode: local.

    ``--check`` is the CI form: it computes the same manifest and exits 5 when the committed one
    does not already match, so a record edited without a rebuild fails the build (prompt standards
    §3) instead of shipping hashes that describe prompts nobody installed.

    The shipped pack is rebuilt, never the user's override directory: an override deliberately
    differs from the manifest and is marked on every result that used it (prompt standards §6).

    Example:
        freeweight prompts build --output ./prompts.lock.json
    """
    from freeweight.services.prompts import (
        PACK_ROOT,
        PromptPackInvalid,
        build_manifest,
        write_manifest,
    )

    try:
        manifest, drift = build_manifest(PACK_ROOT, generated_at=generated_at)
    except PromptPackInvalid as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc

    written = not (check or dry_run)
    if written:
        write_manifest(manifest, PACK_ROOT)

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "pack_root": str(PACK_ROOT),
                    "current": drift.is_current,
                    "written": written,
                    "added": [list(pair) for pair in drift.added],
                    "removed": [list(pair) for pair in drift.removed],
                    "changed": [list(pair) for pair in drift.changed],
                    "pack_sha256": manifest["pack_sha256"],
                }
            )
        )
    else:
        for label, pairs in (
            ("added", drift.added),
            ("removed", drift.removed),
            ("changed", drift.changed),
        ):
            for prompt_id, version in pairs:
                typer.echo(f"  {label}: {prompt_id} v{version}")
        if drift.is_current:
            typer.echo(f"manifest is current: {manifest['pack_sha256']}")
        elif written:
            typer.echo(f"manifest rebuilt: {manifest['pack_sha256']}")
        else:
            typer.echo(f"manifest is stale; would become {manifest['pack_sha256']}")

    if check and not drift.is_current:
        # Exit 5, "the operation executed and did not succeed" (CLI standards §4): the check ran
        # and its answer is no. Exit 2 would claim the invocation was wrong, and 3 would claim the
        # pack is unusable — it is usable, it is merely undescribed.
        raise typer.Exit(5)
