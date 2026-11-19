"""The MCP catalogue, grants, and what reaches an agent's `config.yaml` (FLOTTA-125)."""

from __future__ import annotations

import json

import pytest
import yaml

from flotta.box import rules as box_rules
from flotta.mcp import (
    Grant,
    McpError,
    config_changes,
    mcp_base,
    normalized,
    parse_catalogue,
    parse_grants,
    parse_key,
)

LINEAR = {
    "name": "linear",
    "title": "Linear",
    "url": "https://mcp.linear.app/mcp",
    "auth": "oauth",
    "tools": ["list_issues", "create_issue"],
}
NOTION = {"name": "notion", "url": "https://mcp.notion.com/mcp", "auth": "bearer"}
SERVERS = parse_catalogue(json.dumps([LINEAR, NOTION]))


def test_the_catalogue_reads_the_operators_servers():
    assert set(SERVERS) == {"linear", "notion"}
    assert SERVERS["linear"].tools == ("list_issues", "create_issue")
    assert SERVERS["notion"].title == "notion" and SERVERS["notion"].ask_first is True
    assert parse_catalogue(None) == {} and parse_catalogue("  ") == {}


@pytest.mark.parametrize(
    ("entry", "says"),
    [
        ({**NOTION, "url": "http://mcp.notion.com/mcp"}, "https"),
        ({**NOTION, "url": "https://user:pw@mcp.notion.com/mcp"}, "user or password"),
        ({**NOTION, "auth": "magic"}, "auth is one of"),
        ({**NOTION, "name": "Notion"}, "lowercase"),
        ({**NOTION, "auth": "api_key"}, "names the header"),
        ({**NOTION, "auth": "api_key", "header": "Cookie"}, "can't go in"),
        ({**NOTION, "header": "X-Key"}, "only an api_key"),
        ({**NOTION, "tools": ["read_*"]}, "tool names"),
        ({**NOTION, "command": "npx thing"}, "unknown fields"),
        ({**LINEAR, "oauth": {"client_secret_env": "FLOTTA_SIGNING_KEY"}}, "FLOTTA_MCP_"),
    ],
)
def test_an_entry_that_cant_be_used_is_refused(entry, says):
    with pytest.raises(McpError, match=says):
        parse_catalogue(json.dumps([entry]))


def test_a_local_server_may_be_plain_http():
    found = parse_catalogue(json.dumps([{**NOTION, "url": "http://127.0.0.1:9000/mcp"}]))
    assert found["notion"].url == "http://127.0.0.1:9000/mcp"


def test_a_name_twice_is_refused():
    with pytest.raises(McpError, match="twice"):
        parse_catalogue(json.dumps([NOTION, NOTION]))


def test_grants_read_from_a_body():
    grants = parse_grants(
        {"servers": [{"server": "linear", "tools": ["list_issues"], "ask_first": False}]}, SERVERS
    )
    assert grants == (Grant("linear", ("list_issues",), False),)
    defaulted = parse_grants({"servers": [{"server": "notion"}]}, SERVERS)
    assert defaulted == (Grant("notion", None, True),)


@pytest.mark.parametrize(
    ("body", "says"),
    [
        ({"servers": [{"server": "github"}]}, "not one of"),
        ({"servers": [{"server": "linear", "tools": ["delete_workspace"]}]}, "doesn't offer"),
        ({"servers": [{"server": "notion", "tools": ["*"]}]}, "tool names"),
        ({"servers": [{"server": "notion", "tools": []}]}, "list of tool names"),
        ({"servers": [{"server": "notion"}, {"server": "notion"}]}, "twice"),
        ({"servers": [{"server": "notion", "trust": "full"}]}, "each grant"),
        ({"grants": []}, "servers"),
    ],
)
def test_a_grant_that_cant_be_kept_is_refused(body, says):
    with pytest.raises(McpError, match=says):
        parse_grants(body, SERVERS)


def test_a_grant_allows_its_tools_and_the_catalogue_bounds_one_that_names_none():
    linear, notion = SERVERS["linear"], SERVERS["notion"]
    assert Grant("linear", ("list_issues",), True).allows(linear, "list_issues")
    assert not Grant("linear", ("list_issues",), True).allows(linear, "create_issue")
    assert Grant("linear", None, True).allows(linear, "create_issue")
    assert not Grant("linear", None, True).allows(linear, "delete_workspace")
    assert Grant("notion", None, True).allows(notion, "anything_it_lists")


def _written(config, changes, tmp_path):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
    box_rules.apply(tmp_path, changes)
    return yaml.safe_load((tmp_path / "config.yaml").read_text())


def test_a_grant_becomes_an_entry_pointing_at_the_gateway_with_the_boxs_own_token(tmp_path):
    changes = config_changes(
        [Grant("linear", ("list_issues",), True)],
        SERVERS,
        base="https://gw.example",
        token="flotta_box_token",
    )
    config = _written({"model": {"default": "x"}}, changes, tmp_path)
    assert config["mcp_servers"] == {
        "flotta-linear": {
            "url": "https://gw.example/mcp/linear",
            "headers": {"Authorization": "Bearer flotta_box_token"},
            "tools": {"include": ["list_issues"]},
            "trust": "untrusted",
        }
    }
    assert config["model"] == {"default": "x"}


def test_an_empty_grant_on_a_dynamic_connector_exposes_zero_tools(tmp_path):
    """`()` is a filter of nothing. Omitting `tools` would be no filter at all."""
    from flotta.mcp_library import catalogue_entry, entry

    linear = parse_catalogue(json.dumps([catalogue_entry(entry("linear"), {})]))
    grants = parse_grants({"servers": [{"server": "linear"}]}, linear)
    assert grants[0].tools == ()
    config = _written(
        {},
        config_changes(list(grants), linear, base="https://gw.example", token="t"),
        tmp_path,
    )
    assert config["mcp_servers"]["flotta-linear"]["tools"] == {"include": []}


def test_a_grant_taken_away_leaves_no_entry_and_the_agents_own_stay(tmp_path):
    before = {
        "mcp_servers": {
            "flotta-notion": {"url": "https://gw.example/mcp/notion"},
            "flotta-retired": {"url": "https://gw.example/mcp/retired"},
            "mine": {"command": "my-server"},
        }
    }
    changes = config_changes(
        [Grant("linear", None, False)],
        SERVERS,
        base="https://gw.example",
        token="t",
        previous=["notion", "retired"],
    )
    config = _written(before, changes, tmp_path)
    assert set(config["mcp_servers"]) == {"flotta-linear", "mine"}
    linear = config["mcp_servers"]["flotta-linear"]
    assert "trust" not in linear
    assert linear["tools"] == {"include": ["list_issues", "create_issue"]}


def test_no_grants_at_all_takes_the_section_away(tmp_path):
    changes = config_changes([], SERVERS, base="https://gw.example", token="t")
    assert "mcp_servers" not in _written({"mcp_servers": {"flotta-linear": {}}}, changes, tmp_path)


def test_nothing_of_the_upstream_reaches_the_config():
    changes = config_changes(
        [Grant("linear", None, True)], SERVERS, base="https://gw.example", token="t"
    )
    assert "linear.app" not in json.dumps(changes)


@pytest.mark.parametrize(
    ("url", "base"),
    [
        ("https://gw.example/v1", "https://gw.example"),
        ("https://gw.example/", "https://gw.example"),
    ],
)
def test_the_gateway_base_drops_the_models_path(url, base):
    assert mcp_base({"FLOTTA_GATEWAY_URL": url}) == base
    assert mcp_base({}) is None


@pytest.mark.parametrize("key", ["", "  ", "a\nb", "x" * 5000])
def test_a_key_that_cant_go_in_a_header_is_refused(key):
    with pytest.raises(McpError):
        parse_key({"key": key}, SERVERS["notion"])


def test_an_oauth_server_takes_no_key():
    with pytest.raises(McpError, match="signing in"):
        parse_key({"key": "k"}, SERVERS["linear"])


# -- the console's catalogue, and asking a running agent to reload ---------------------


class _Machine:
    def __init__(self, reload_code=0, fail_reload=False, hermes_starts=True):
        self.commands: list[str] = []
        self.reload_code, self.fail_reload = reload_code, fail_reload
        #: Whether a wait for Hermes hears it, and what happens meanwhile (FLOTTA-252).
        self.hermes_starts = hermes_starts
        self.while_booting = lambda: None

    def exec(self, endpoint, command):
        from flotta.backend import ExecResult
        from flotta.box.mcp_reload import ONLY_WAIT_FLAG
        from flotta.provision import MCP_RELOAD_MARK

        self.commands.append(command)
        if command.endswith(ONLY_WAIT_FLAG):
            self.while_booting()
            if self.hermes_starts:
                return ExecResult(0, "Hermes is listening", "")
            return ExecResult(1, "", "Hermes was not listening after 120s")
        if MCP_RELOAD_MARK in command:
            if self.fail_reload:
                raise RuntimeError("machine went away")
            return ExecResult(self.reload_code, "", "refused" if self.reload_code else "")
        return ExecResult(0, "config written", "")

    # Enough of a substrate for `wake_box`: asleep until started.
    asleep = False

    def state(self, endpoint):
        return "stopped" if self.asleep else "started"

    def start(self, endpoint):
        self.commands.append("start")
        self.asleep = False

    def configs(self):
        from flotta.provision import MCP_RELOAD_MARK

        return [
            c for c in self.commands if c not in ("start", "create") and MCP_RELOAD_MARK not in c
        ]


@pytest.fixture
def granted(tmp_path, monkeypatch):
    from flotta.store import FleetStore

    monkeypatch.setenv("FLOTTA_SIGNING_KEY", "k")
    monkeypatch.setenv("FLOTTA_GATEWAY_URL", "https://gw.example.test/v1")
    store = FleetStore(tmp_path / "fleet.db")
    store.set_setting(
        "FLOTTA_MCP_SERVERS",
        normalized(
            json.dumps([{"name": "notes", "url": "https://n.example.test", "auth": "bearer"}])
        ),
    )
    store.create_box("one", box_id="b-one")
    yield store
    store.close()


def _events(store, kind):
    return [e for e in store.get_box_timeline("b-one") if e.type == kind]


def test_a_running_agent_is_asked_to_reload_after_its_grants_are_written(granted):
    from flotta.provision import MCP_RELOAD_MARK, set_mcp_grants

    granted.update_box_status("b-one", "running", endpoint="fake://m")
    machine = _Machine()
    answer = set_mcp_grants("b-one", (Grant("notes", None, True),), store=granted, backend=machine)
    assert answer["written"] is True and answer["reloaded"] is True
    assert MCP_RELOAD_MARK in machine.commands[-1]
    assert MCP_RELOAD_MARK not in machine.commands[0]
    assert _events(granted, "mcp_reloaded")


def test_an_agent_that_isnt_running_isnt_woken_to_reload(granted):
    from flotta.provision import MCP_RELOAD_MARK, set_mcp_grants

    granted.update_box_status("b-one", "running", endpoint="fake://m")
    granted.update_box_status("b-one", "stopped")
    machine = _Machine()
    answer = set_mcp_grants("b-one", (), store=granted, backend=machine)
    assert answer["reloaded"] is None
    assert not any(MCP_RELOAD_MARK in c for c in machine.commands)


@pytest.mark.parametrize("machine", [_Machine(reload_code=1), _Machine(fail_reload=True)])
def test_a_reload_that_fails_is_recorded_and_the_grant_stands(granted, machine):
    from flotta.provision import set_mcp_grants

    granted.update_box_status("b-one", "running", endpoint="fake://m")
    answer = set_mcp_grants("b-one", (Grant("notes", None, True),), store=granted, backend=machine)
    assert answer["written"] is True and answer["reloaded"] is False
    assert [g["server"] for g in answer["servers"]] == ["notes"]
    assert _events(granted, "mcp_reload_failed")


def test_the_reload_script_carries_no_credential():
    """It reads the box's own dashboard login where it runs; nothing is sent in."""
    from flotta.provision import _mcp_reload_command

    command = _mcp_reload_command()
    assert "PASSWORD=" not in command and "Bearer" not in command


def test_a_client_secret_is_never_in_the_setting():
    """The setting is shown to the operator and copied about; a secret has its own table."""
    raw = json.dumps(
        [
            {
                "name": "t",
                "url": "https://t.example.test",
                "auth": "oauth",
                "oauth": {"client_id": "c", "client_secret": "s"},
            }
        ]
    )
    with pytest.raises(McpError, match="unknown oauth fields: client_secret"):
        normalized(raw)


def test_a_substrate_with_no_shell_has_nothing_to_reload(granted):
    """`just local`'s stand-in: not a failed reload, since there is no Hermes to ask."""
    from flotta.backend import ExecResult, NotSupported
    from flotta.provision import MCP_RELOAD_MARK, set_mcp_grants

    class _NoShell:
        def exec(self, endpoint, command):
            if MCP_RELOAD_MARK in command:
                raise NotSupported("a local stand-in has no shell")
            return ExecResult(0, "config written", "")

    granted.update_box_status("b-one", "running", endpoint="fake://m")
    answer = set_mcp_grants(
        "b-one", (Grant("notes", None, True),), store=granted, backend=_NoShell()
    )
    assert answer["written"] is True and answer["reloaded"] is None
    assert not _events(granted, "mcp_reload_failed")


# -- a catalogue change rewrites configs, and never grants (review of #188) ------------


def _grant_notes(store, machine):
    from flotta.provision import set_mcp_grants

    store.update_box_status("b-one", "running", endpoint="fake://m")
    set_mcp_grants("b-one", (Grant("notes", None, True),), store=store, backend=machine)
    machine.commands.clear()


def test_a_catalogue_rewrite_writes_no_grant_and_a_revoke_meanwhile_stays(granted, monkeypatch):
    """The agency revokes between the rewrite finding the box and taking its lock:
    the rewrite reads the grants under the lock, so the revoke stands."""
    import flotta.provision as provision
    from flotta.provision import rewrite_mcp_configs

    machine = _Machine()
    _grant_notes(granted, machine)
    real_lock = provision._rules_lock
    revoke = granted.set_box_mcp_grants

    def revoking_first(box_id):
        revoke(box_id, [])  # the agency's save lands first
        return real_lock(box_id)

    writes = []
    monkeypatch.setattr(provision, "_rules_lock", revoking_first)
    monkeypatch.setattr(granted, "set_box_mcp_grants", lambda *a, **k: writes.append(a))
    rewrite_mcp_configs(["notes"], store=granted, backend=machine)
    assert writes == []
    assert granted.box_mcp_grants("b-one") == []
    payload = machine.configs()[-1].rsplit(" ", 1)[1]
    import base64

    changes = json.loads(base64.b64decode(payload))
    assert ["mcp_servers", "flotta-notes", None] in changes


def test_a_stopped_agent_isnt_woken_by_a_catalogue_change_and_gets_it_at_wake(granted):
    from flotta.provision import rewrite_mcp_configs, wake_box

    machine = _Machine()
    _grant_notes(granted, machine)
    granted.update_box_status("b-one", "stopped")
    machine.asleep = True
    assert rewrite_mcp_configs(["notes"], store=granted, backend=machine) == {"b-one": None}
    assert machine.commands == []
    assert granted.mcp_config_pending("b-one")

    wake_box("b-one", store=granted, backend=machine)
    assert machine.commands[0] == "start"
    assert len(machine.configs()) == 1
    assert not granted.mcp_config_pending("b-one")


def _reloads(machine):
    from flotta.box.mcp_reload import ONLY_WAIT_FLAG
    from flotta.provision import MCP_RELOAD_MARK

    return [c for c in machine.commands if MCP_RELOAD_MARK in c and not c.endswith(ONLY_WAIT_FLAG)]


def test_a_wake_that_starts_the_machine_waits_for_hermes_to_reload(granted):
    """Seen live on eng-r: a reload sent while Hermes booted was a failure that wasn't."""
    from flotta.box.mcp_reload import WAIT_FLAG
    from flotta.provision import set_mcp_grants, wake_box

    granted.update_box_status("b-one", "running", endpoint="fake://m")
    granted.update_box_status("b-one", "stopped")
    machine = _Machine()
    machine.asleep = True
    set_mcp_grants("b-one", (Grant("notes", None, True),), store=granted, backend=machine)
    wake_box("b-one", store=granted, backend=machine)
    (reload,) = _reloads(machine)
    assert reload.endswith(WAIT_FLAG)
    assert _events(granted, "mcp_reloaded") and not _events(granted, "mcp_reload_failed")


def test_a_reload_on_a_machine_already_up_does_not_wait(granted):
    from flotta.box.mcp_reload import WAIT_FLAG
    from flotta.provision import set_mcp_grants

    granted.update_box_status("b-one", "running", endpoint="fake://m")
    machine = _Machine()
    set_mcp_grants("b-one", (Grant("notes", None, True),), store=granted, backend=machine)
    (reload,) = _reloads(machine)
    assert not reload.endswith(WAIT_FLAG)


def test_a_grant_saved_for_a_stopped_agent_is_in_force_and_written_at_wake(granted):
    """Saved now, enforced at the gateway now, written to the agent when it next wakes."""
    from flotta.provision import set_mcp_grants, wake_box

    granted.update_box_status("b-one", "running", endpoint="fake://m")
    granted.update_box_status("b-one", "stopped")
    machine = _Machine()
    machine.asleep = True
    answer = set_mcp_grants("b-one", (Grant("notes", None, True),), store=granted, backend=machine)
    assert (answer["written"], answer["pending"]) == (False, True)
    assert "asleep" in answer["detail"]
    assert machine.commands == []
    assert [g.server for g in granted.box_mcp_grants("b-one")] == ["notes"]

    wake_box("b-one", store=granted, backend=machine)
    assert len(machine.configs()) == 1
    assert not granted.mcp_config_pending("b-one")


def test_a_wake_with_nothing_pending_writes_nothing(granted):
    from flotta.provision import wake_box

    granted.update_box_status("b-one", "running", endpoint="fake://m")
    granted.update_box_status("b-one", "stopped")
    machine = _Machine()
    machine.asleep = True
    wake_box("b-one", store=granted, backend=machine)
    assert machine.commands == ["start"]


# -- tools chosen before the agent can be written (FLOTTA-252) --------------------------


class _Creating(_Machine):
    """`_Machine`, and enough of a substrate for `create_box` to make one."""

    def create(self, spec):
        from flotta.backend import BoxHandle

        self.commands.append("create")
        return BoxHandle(id="m1", endpoint="fake://m")

    def apply_secrets(self, endpoint, secrets):
        pass


def _waits(machine):
    from flotta.box.mcp_reload import ONLY_WAIT_FLAG

    return [c for c in machine.commands if c.endswith(ONLY_WAIT_FLAG)]


def _create(store, machine):
    from flotta.provision import create_box

    return create_box("one", store=store, backend=machine, box=store.get_box("b-one"))


NOTES = (Grant("notes", None, True),)


def test_tools_given_while_an_agent_is_being_created_wait_for_its_machine(granted):
    """Recorded and never written, by anything, until saved a second time: that was the bug."""
    import flotta.provision as provision
    from flotta.provision import set_mcp_grants

    machine = _Creating()
    answer = set_mcp_grants("b-one", NOTES, store=granted, backend=machine)
    assert (answer["written"], answer["pending"], answer["waits_for"]) == (False, True, "created")
    assert "still being created" in answer["detail"]
    assert machine.commands == []
    assert granted.mcp_config_pending("b-one")
    assert not _events(granted, "mcp_grants_write_failed")

    _create(granted, machine)
    assert granted.get_box("b-one").status == "running"
    assert len(machine.configs()) == 1
    assert not granted.mcp_config_pending("b-one")
    assert [e.payload["reason"] for e in _events(granted, "mcp_config_rewritten")] == ["created"]
    assert not provision._BOOTING


def test_a_new_agents_config_is_not_written_until_hermes_listens(granted):
    """Its first boot edits the same file with no lock, and Hermes starts after it. By
    then Hermes has read the file, so it is asked to reload, and has nothing to wait for."""
    from flotta.box.mcp_reload import WAIT_FLAG
    from flotta.provision import set_mcp_grants

    machine = _Creating()
    set_mcp_grants("b-one", NOTES, store=granted, backend=machine)
    _create(granted, machine)
    (wait,), (config,), (reload,) = _waits(machine), machine.configs(), _reloads(machine)
    order = [machine.commands.index(c) for c in (wait, config, reload)]
    assert order == sorted(order), "the config was written before the boot was known to be over"
    assert not reload.endswith(WAIT_FLAG)
    assert _events(granted, "mcp_reloaded")


def test_an_agent_created_with_no_tools_chosen_is_sent_nothing(granted):
    import flotta.provision as provision

    machine = _Creating()
    _create(granted, machine)
    assert machine.commands == ["create", "start"]
    assert not provision._BOOTING


def test_a_new_agent_whose_hermes_never_listens_keeps_its_tools_for_a_wake(granted):
    """Nothing is written into a boot that isn't known to be over; the mark stays."""
    import flotta.provision as provision
    from flotta.provision import set_mcp_grants, wake_box

    machine = _Creating(hermes_starts=False)
    set_mcp_grants("b-one", NOTES, store=granted, backend=machine)
    made = _create(granted, machine)
    assert made["status"] == "running"
    assert machine.configs() == [] and _reloads(machine) == []
    assert granted.mcp_config_pending("b-one")
    (failed,) = _events(granted, "mcp_grants_write_failed")
    assert "Hermes did not start" in failed.payload["detail"]
    assert not provision._BOOTING

    wake_box("b-one", store=granted, backend=machine)
    assert len(machine.configs()) == 1 and not granted.mcp_config_pending("b-one")


def test_a_substrate_with_no_shell_has_no_boot_to_wait_out(granted):
    """`just local`'s stand-in: nothing edits its config but the writer, so it is written."""
    from flotta.backend import NotSupported
    from flotta.box.mcp_reload import ONLY_WAIT_FLAG
    from flotta.provision import MCP_RELOAD_MARK, set_mcp_grants

    class _NoShell(_Creating):
        def exec(self, endpoint, command):
            if MCP_RELOAD_MARK in command:
                self.commands.append(command)
                raise NotSupported("a local stand-in has no shell")
            return super().exec(endpoint, command)

    machine = _NoShell()
    set_mcp_grants("b-one", NOTES, store=granted, backend=machine)
    _create(granted, machine)
    assert len(machine.configs()) == 1 and not granted.mcp_config_pending("b-one")
    assert machine.commands[2].endswith(ONLY_WAIT_FLAG)
    assert not _events(granted, "mcp_grants_write_failed")


def test_a_save_while_a_new_agent_boots_waits_too_and_is_in_what_is_written(granted):
    """The row says running, and its boot is still editing the file: not written now."""
    import base64

    from flotta.provision import set_mcp_grants

    machine = _Creating()
    set_mcp_grants("b-one", NOTES, store=granted, backend=machine)
    seen = {}

    def a_second_save():
        assert granted.get_box("b-one").status == "running"
        seen["answer"] = set_mcp_grants(
            "b-one", (Grant("notes", ("search",), False),), store=granted, backend=machine
        )
        seen["sent"] = list(machine.configs())

    machine.while_booting = a_second_save
    _create(granted, machine)
    answer = seen["answer"]
    assert (answer["written"], answer["pending"], answer["waits_for"]) == (False, True, "created")
    assert seen["sent"] == [], "a config was written into a boot that was not over"
    (config,) = machine.configs()
    changes = json.loads(base64.b64decode(config.rsplit(" ", 1)[1]))
    (entry,) = [value for _, key, value in changes if key == "flotta-notes"]
    assert entry["tools"] == {"include": ["search"]} and "trust" not in entry


def test_a_conversation_opened_while_a_new_agent_boots_writes_nothing(granted):
    """The door wakes an agent for every conversation, and a wake writes a pending config."""
    from flotta.provision import set_mcp_grants, wake_box

    machine = _Creating()
    set_mcp_grants("b-one", NOTES, store=granted, backend=machine)
    seen = {}

    def someone_opens_it():
        woke = wake_box("b-one", store=granted, backend=machine)
        seen["status"], seen["sent"] = woke["status"], list(machine.configs())

    machine.while_booting = someone_opens_it
    _create(granted, machine)
    assert seen == {"status": "running", "sent": []}
    assert len(machine.configs()) == 1 and not granted.mcp_config_pending("b-one")


def test_a_creation_that_fails_to_write_the_config_still_made_the_agent(granted, monkeypatch):
    """Best effort: the grants are in force at the gateway, and the mark stays for a wake."""
    import flotta.provision as provision
    from flotta.provision import set_mcp_grants

    machine = _Creating()
    set_mcp_grants("b-one", NOTES, store=granted, backend=machine)

    def broken(*args, **kwargs):
        raise RuntimeError("nothing answered")

    monkeypatch.setattr(provision, "rewrite_mcp_config", broken)
    assert _create(granted, machine)["status"] == "running"
    assert granted.mcp_config_pending("b-one")
    assert not provision._BOOTING


def test_a_creation_that_cant_even_look_for_waiting_tools_still_made_the_agent(
    granted, monkeypatch
):
    import flotta.provision as provision

    def broken(box_id):
        raise RuntimeError("the store went away")

    monkeypatch.setattr(granted, "mcp_config_pending", broken)
    assert _create(granted, _Creating())["status"] == "running"
    assert not provision._BOOTING


def test_creation_looks_for_waiting_tools_only_once_a_save_in_flight_has_left_its_mark(
    granted, tmp_path
):
    """A save that saw an agent still being created holds the lock until it has marked
    it, and creation reads the mark under the same lock: it can't look in between."""
    import threading

    import flotta.provision as provision
    from flotta.store import FleetStore

    granted.update_box_status("b-one", "running", endpoint="fake://m")
    machine, looked = _Machine(), threading.Event()

    def creation():
        with FleetStore(tmp_path / "fleet.db") as own:
            provision._write_mcp_config_that_waited("b-one", store=own, backend=machine)
        looked.set()

    with provision._rules_lock("b-one"):  # the save, between reading the box and its mark
        worker = threading.Thread(target=creation)
        worker.start()
        assert not looked.wait(0.3), "creation looked while a save still held the lock"
        granted.set_box_mcp_grants("b-one", [("notes", None, True)])
        granted.mark_mcp_config_pending("b-one")
    worker.join(10)
    assert looked.is_set()
    assert len(machine.configs()) == 1 and not granted.mcp_config_pending("b-one")


def test_an_agent_with_nothing_waiting_stops_being_new_under_the_lock_a_save_holds(granted):
    """Or a save could see it still booting after creation had looked and gone: marked
    as waiting for a creation that will not come back for it."""
    import flotta.provision as provision

    granted.update_box_status("b-one", "running", endpoint="fake://m")
    provision._BOOTING.add("b-one")
    real, seen = provision._rules_lock("b-one"), []

    class _Watching:
        def __enter__(self):
            real.acquire()

        def __exit__(self, *exc):
            seen.append("b-one" in provision._BOOTING)
            real.release()

    try:
        provision._RULES_LOCKS["b-one"] = _Watching()
        provision._write_mcp_config_that_waited("b-one", store=granted, backend=_Machine())
    finally:
        provision._RULES_LOCKS["b-one"] = real
        provision._BOOTING.discard("b-one")
    assert seen == [False], "it was still new when the lock was let go"


def test_a_save_that_finds_the_machine_up_by_the_time_it_holds_the_lock_writes_to_it(
    granted, monkeypatch
):
    """Decided from the box as it is under the lock, not as it was when the save arrived:
    creation has already looked for a mark by then, and nothing else would come back for one."""
    import flotta.provision as provision
    from flotta.provision import set_mcp_grants

    real_lock = provision._rules_lock

    def created_first(box_id):
        if granted.get_box(box_id).status == "provisioning":
            granted.update_box_status(box_id, "running", endpoint="fake://m")
        return real_lock(box_id)

    monkeypatch.setattr(provision, "_rules_lock", created_first)
    machine = _Machine()
    answer = set_mcp_grants("b-one", NOTES, store=granted, backend=machine)
    assert (answer["written"], answer["pending"], answer["waits_for"]) == (True, False, None)
    assert answer["reloaded"] is True
    assert len(machine.configs()) == 1 and not granted.mcp_config_pending("b-one")


def test_tools_given_to_a_paused_agent_are_written_when_it_next_wakes(granted):
    """Not started for this, which is what paused means, and not lost either."""
    from flotta.provision import set_mcp_grants, wake_box

    granted.update_box_status("b-one", "running", endpoint="fake://m")
    granted.update_box_status("b-one", "paused")
    machine = _Machine()
    machine.asleep = True
    answer = set_mcp_grants("b-one", NOTES, store=granted, backend=machine)
    assert (answer["written"], answer["pending"], answer["waits_for"]) == (False, True, "woken")
    assert "paused" in answer["detail"]
    assert machine.commands == []
    assert not _events(granted, "mcp_grants_write_failed")

    granted.update_box_status("b-one", "stopped")  # the budget lets go
    wake_box("b-one", store=granted, backend=machine)
    assert len(machine.configs()) == 1 and not granted.mcp_config_pending("b-one")


def test_tools_given_while_the_org_is_suspended_wait_without_touching_the_machine(granted):
    from flotta.provision import set_mcp_grants

    granted.create_org("agency-x", "Agency X")
    granted.create_client("agency-x", "acme", "Acme")
    granted.create_box("two", box_id="b-two", org="agency-x", client="acme")
    granted.update_box_status("b-two", "running", endpoint="fake://m2")
    granted.suspend_org("agency-x")
    machine = _Machine()
    answer = set_mcp_grants("b-two", NOTES, store=granted, backend=machine)
    assert (answer["written"], answer["pending"], answer["waits_for"]) == (False, True, "woken")
    assert "suspended" in answer["detail"]
    assert machine.commands == [] and granted.mcp_config_pending("b-two")


def test_tools_given_to_an_agent_that_is_gone_wait_for_nothing(granted):
    from flotta.provision import set_mcp_grants

    granted.update_box_status("b-one", "torn_down")
    machine = _Machine()
    answer = set_mcp_grants("b-one", NOTES, store=granted, backend=machine)
    assert (answer["written"], answer["pending"], answer["waits_for"]) == (False, False, None)
    assert "torn_down" in answer["detail"]
    assert not granted.mcp_config_pending("b-one")
    assert _events(granted, "mcp_grants_write_failed")


def test_a_wake_still_writes_once_and_reloads_once(granted):
    """Nothing about a wake changed: one config, one reload that waits for Hermes."""
    from flotta.provision import set_mcp_grants, wake_box

    granted.update_box_status("b-one", "running", endpoint="fake://m")
    granted.update_box_status("b-one", "stopped")
    machine = _Machine()
    machine.asleep = True
    set_mcp_grants("b-one", NOTES, store=granted, backend=machine)
    wake_box("b-one", store=granted, backend=machine)
    assert (len(machine.configs()), len(_reloads(machine)), _waits(machine)) == (1, 1, [])


class _MovingMachine(_Machine):
    """`_Machine`, plus the secrets a gateway move writes and removes."""

    def apply_secrets(self, endpoint, secrets):
        self.commands.append("apply")

    def remove_secrets(self, endpoint, names):
        self.commands.append("remove")


def test_joining_a_moved_gateway_points_the_agents_mcp_servers_at_it(granted, monkeypatch):
    """The gateway left Render for Fly (7 Nov 2026). An agent joined again must not keep
    calling its MCP servers at the old address, which its config names."""
    from flotta.provision import join_gateway

    granted.update_box_status("b-one", "running", endpoint="fake://m")
    granted.set_box_model("b-one", "z-ai/glm-5.2")
    granted.set_box_mcp_grants("b-one", [("notes", None, False)])
    monkeypatch.setenv("FLOTTA_GATEWAY_URL", "http://flotta-gateway.internal:8080")
    machine = _MovingMachine()
    join_gateway("b-one", store=granted, backend=machine)

    assert machine.commands[:2] == ["apply", "remove"]
    (config,) = machine.configs()[2:]
    import base64

    changes = json.dumps(json.loads(base64.b64decode(config.rsplit(" ", 1)[1])))
    assert "http://flotta-gateway.internal:8080/mcp/notes" in changes
    assert "gw.example.test" not in changes
    assert _events(granted, "mcp_config_rewritten")


def test_joining_leaves_a_sleeping_agents_mcp_config_for_its_wake(granted):
    from flotta.provision import join_gateway

    granted.update_box_status("b-one", "running", endpoint="fake://m")
    granted.update_box_status("b-one", "stopped")
    granted.set_box_model("b-one", "z-ai/glm-5.2")
    granted.set_box_mcp_grants("b-one", [("notes", None, False)])
    machine = _MovingMachine()
    join_gateway("b-one", store=granted, backend=machine)
    assert machine.configs() == ["apply", "remove"]
    assert granted.mcp_config_pending("b-one")


def test_joining_an_agent_with_no_mcp_grants_writes_no_mcp_config(granted):
    from flotta.provision import join_gateway

    granted.update_box_status("b-one", "running", endpoint="fake://m")
    granted.set_box_model("b-one", "z-ai/glm-5.2")
    machine = _MovingMachine()
    join_gateway("b-one", store=granted, backend=machine)
    assert machine.commands == ["apply", "remove"]
