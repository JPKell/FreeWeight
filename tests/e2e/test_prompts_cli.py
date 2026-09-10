"""``freeweight prompts show --json`` carries the whole shipped record (row W9).

WeightRoomGym's prompt editor starts an override from this record and diffs the override against
it, so the field has to be the record exactly as installed — every key, not the summary the rest of
the payload is.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from freeweight.cli.main import app
from freeweight.services.prompts import PACK_ROOT

runner = CliRunner()


def test_prompts_show_json_carries_the_shipped_record_whole() -> None:
    listed = runner.invoke(app, ["prompts", "list", "--json"])
    assert listed.exit_code == 0, listed.output
    first = json.loads(listed.output)["prompts"][0]

    result = runner.invoke(app, ["prompts", "show", first["prompt_id"], "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    record = payload["record"]
    assert (record["prompt_id"], record["version"]) == (payload["prompt_id"], payload["version"])
    assert record["schema_version"]
    assert record["metadata"]["change_reason"]
    on_disk = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in PACK_ROOT.rglob("*.json")
        if path.name != "manifest.json"
    ]
    assert record in on_disk
