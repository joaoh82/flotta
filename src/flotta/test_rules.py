"""An agent's rules (FLOTTA-124): what is kept, what the agent reads, what Hermes enforces."""

from __future__ import annotations

import subprocess
import sys

import pytest
import yaml

from flotta.box.rules import merge
from flotta.rules import (
    MAX_ITEMS,
    PRIVATE_RULE,
    Rules,
    RulesError,
    config_changes,
    parse_rules,
    prompt_block,
    rules_from_json,
)

FULL = {
    "always": ["Answer in Portuguese", "  Cite   the ticket  "],
    "never": ["Push to main"],
    "careful": ["Production databases"],
    "denied_commands": ["git push --force*", "rm -rf /*"],
    "disabled_tools": ["browser", "terminal"],
    "private_conversations": True,
}


def test_rules_are_kept_tidy_and_in_a_fixed_order():
    rules = parse_rules(FULL)
    assert rules.always == ("Answer in Portuguese", "Cite the ticket")
    # The catalogue's order, whatever order they came in.
    assert rules.disabled_tools == ("terminal", "browser")
    assert rules_from_json(__import__("json").dumps(rules.to_json())) == rules
    assert parse_rules({"always": ["x", "x", " "]}).always == ("x",)


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"always": "one string"},
        {"never": [3]},
        {"careful": ["x" * 301]},
        {"always": [f"rule {i}" for i in range(MAX_ITEMS + 1)]},
        {"disabled_tools": ["shell.exec"]},
        {"private_conversations": "yes"},
        {"approvals": {"mode": "off"}},
    ],
    ids=[
        "not-object",
        "string",
        "number",
        "long",
        "many",
        "unknown-tool",
        "not-bool",
        "unknown-field",
    ],
)
def test_what_cant_be_kept_is_refused(body):
    with pytest.raises(RulesError):
        parse_rules(body)


def test_the_agent_reads_its_guidance_and_nothing_else():
    block = prompt_block(parse_rules(FULL))
    assert block is not None
    for text in (
        "Always:",
        "- Answer in Portuguese",
        "Never:",
        "- Push to main",
        "- Production databases",
    ):
        assert text in block
    assert PRIVATE_RULE in block
    # What is enforced isn't left to the model, and isn't spelled out to it either.
    assert "git push --force" not in block
    assert prompt_block(Rules()) is None


def test_config_holds_every_owned_key_set_or_removed():
    changes = config_changes(parse_rules(FULL)).values
    assert changes[("approvals", "deny")] == ["git push --force*", "rm -rf /*"]
    # Private conversations switch session search off too.
    assert changes[("agent", "disabled_toolsets")] == ["terminal", "browser", "session_search"]
    assert "Production databases" in changes[("approvals", "smart_policy")]
    assert all(value is None for value in config_changes(Rules()).values.values())


def test_writing_keeps_everything_flotta_doesnt_own():
    """`command_allowlist` is what Hermes writes for "always"; it must survive."""
    before = {
        "command_allowlist": ["recursive delete"],
        "approvals": {"timeout": 60, "deny": ["old*"]},
        "agent": {"max_turns": 90},
        "model": {"default": "x"},
    }
    after = merge(before, config_changes(parse_rules(FULL)).to_json())
    assert after["command_allowlist"] == ["recursive delete"]
    assert after["approvals"]["timeout"] == 60
    assert after["approvals"]["deny"] == ["git push --force*", "rm -rf /*"]
    assert after["agent"]["max_turns"] == 90
    assert after["model"] == {"default": "x"}
    cleared = merge(after, config_changes(Rules()).to_json())
    assert "deny" not in cleared["approvals"] and "smart_policy" not in cleared["approvals"]
    assert cleared["agent"] == {"max_turns": 90}


def test_a_section_that_isnt_a_mapping_is_not_overwritten():
    with pytest.raises(ValueError, match="approvals"):
        merge({"approvals": "off"}, config_changes(parse_rules(FULL)).to_json())


def test_the_command_the_box_runs_writes_the_config(tmp_path):
    """The exact command `set_rules` sends, run by a shell, with this Python and
    a temporary agent home standing in for the box's."""
    from flotta.provision import BOX_PYTHON, HERMES_HOME, set_rules
    from flotta.store import FleetStore

    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"command_allowlist": ["recursive delete"], "approvals": {"timeout": 60}})
    )
    sent: list[str] = []

    class Box:
        def exec(self, endpoint, command):
            sent.append(command)
            local = command.replace(BOX_PYTHON, sys.executable).replace(HERMES_HOME, str(tmp_path))
            done = subprocess.run(["sh", "-c", local], capture_output=True, text=True)
            from flotta.backend import ExecResult

            return ExecResult(done.returncode, done.stdout, done.stderr)

    store = FleetStore(tmp_path / "fleet.db")
    box = store.create_box("eng-r")
    store.update_box_status(box.id, "running", endpoint="fake://m-1")
    nasty = parse_rules(
        {
            "never": ["say $(whoami) or `id` or 'quoted' \"too\""],
            **{k: v for k, v in FULL.items() if k != "never"},
        }
    )

    answer = set_rules(box.id, nasty, store=store, backend=Box(), by="owner")

    written = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert written["command_allowlist"] == ["recursive delete"]
    assert written["approvals"]["deny"] == ["git push --force*", "rm -rf /*"]
    assert written["agent"]["disabled_toolsets"] == ["terminal", "browser", "session_search"]
    assert "say $(whoami)" in written["agent"]["system_prompt"]
    for fragment in ("$(whoami)", "`id`", "'quoted'", "Portuguese"):
        assert fragment not in sent[0], f"{fragment!r} reached the shell verbatim"
    assert answer["rules"]["never"] == list(nasty.never)
    assert answer["changed_at"] is not None
    events = [e for e in store.get_box_timeline(box.id) if e.type == "rules_changed"]
    assert events and events[-1].payload["by"] == "owner"

    set_rules(box.id, Rules(), store=store, backend=Box())
    cleared = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert "deny" not in cleared["approvals"] and "agent" not in cleared
    assert store.box_rules(box.id) is None
    store.close()


def test_a_failed_write_records_nothing_as_changed(tmp_path):
    from flotta.backend import ExecResult
    from flotta.provision import ProvisionError, set_rules
    from flotta.store import FleetStore

    class Broken:
        def exec(self, endpoint, command):
            return ExecResult(1, "", "rules not written: approvals in config.yaml is not a mapping")

    store = FleetStore(tmp_path / "fleet.db")
    box = store.create_box("eng-r")
    store.update_box_status(box.id, "running", endpoint="fake://m-1")
    with pytest.raises(ProvisionError, match="not a mapping"):
        set_rules(box.id, parse_rules(FULL), store=store, backend=Broken())
    assert store.box_rules(box.id) is None
    kinds = [e.type for e in store.get_box_timeline(box.id)]
    assert "rules_write_failed" in kinds and "rules_changed" not in kinds
    store.close()


def test_a_command_pattern_keeps_its_spacing():
    """`a  b` and `a b` match different commands, so a pattern is only trimmed."""
    from fnmatch import fnmatch

    rules = parse_rules({"denied_commands": ["  *customer  exports*  "], "never": ["a   b"]})
    assert rules.denied_commands == ("*customer  exports*",)
    assert rules.never == ("a b",)
    command = "cat '/data/customer  exports/secret.txt'"
    assert fnmatch(command.lower(), rules.denied_commands[0])
    with pytest.raises(RulesError, match="one line"):
        parse_rules({"denied_commands": ["rm *\nls"]})


def test_a_tool_the_catalogue_lost_stays_off_and_readable():
    """Switched off when it was in the list, and still off on the machine."""
    import json

    stored = json.dumps({"disabled_tools": ["terminal", "retired_tool"]})
    rules = rules_from_json(stored)
    assert rules.disabled_tools == ("terminal", "retired_tool")
    kept = parse_rules({"disabled_tools": ["retired_tool"]}, kept_tools=rules.disabled_tools)
    assert kept.disabled_tools == ("retired_tool",)
    with pytest.raises(RulesError):
        parse_rules({"disabled_tools": ["another_tool"]}, kept_tools=rules.disabled_tools)


def test_writers_on_one_box_land_one_after_the_other(tmp_path):
    """Two at once used to share a staged file: one renamed the other's bytes."""
    import base64
    import json
    from pathlib import Path

    from flotta.box import rules as box_rules

    script = Path(box_rules.__file__)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"command_allowlist": ["x"]}))
    payloads = []
    for i in range(12):
        changes = config_changes(parse_rules({"denied_commands": [f"cmd{i} *"]})).to_json()
        payloads.append(base64.b64encode(json.dumps(changes).encode()).decode())
    runs = [
        subprocess.Popen(
            [sys.executable, str(script), str(tmp_path), p],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for p in payloads
    ]
    codes = [run.wait() for run in runs]
    assert codes == [0] * len(runs), [r.stderr.read() for r in runs]
    final = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert final["command_allowlist"] == ["x"]
    assert final["approvals"]["deny"][0] in {f"cmd{i} *" for i in range(12)}
    assert not list(tmp_path.glob(".config.yaml.*.flotta")), "a staged file was left behind"


def test_the_store_records_the_save_the_machine_holds(tmp_path):
    """Saves to one box don't overlap, from the write to its record."""
    import threading
    import time

    from flotta.backend import ExecResult
    from flotta.provision import set_rules
    from flotta.store import FleetStore

    path = tmp_path / "fleet.db"
    with FleetStore(path) as store:
        box = store.create_box("eng-r")
        store.update_box_status(box.id, "running", endpoint="fake://m-1")
    on_machine: list[str] = []
    busy = threading.Lock()

    class Slow:
        def exec(self, endpoint, command):
            assert busy.acquire(blocking=False), "two writes overlapped"
            try:
                time.sleep(0.05)
                on_machine.append(command)
            finally:
                busy.release()
            return ExecResult(0, "", "")

    errors: list[BaseException] = []

    def save(pattern):
        try:
            with FleetStore(path) as store:
                rules = parse_rules({"denied_commands": [pattern]})
                set_rules(box.id, rules, store=store, backend=Slow())
        except BaseException as exc:  # a thread's failure is otherwise silent
            errors.append(exc)

    threads = [threading.Thread(target=save, args=(f"p{i}*",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with FleetStore(path) as store:
        recorded = __import__("json").loads(store.box_rules(box.id)[0])["denied_commands"][0]
    import base64
    import json

    last = json.loads(base64.b64decode(on_machine[-1].rsplit(" ", 1)[1]))
    assert [v for s, k, v in last if k == "deny"] == [[recorded]]
