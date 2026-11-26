"""The operator changes a template; whoever makes agents reads it (FLOTTA-269)."""

from __future__ import annotations

from flotta.control.test_mcp_api import api, machine, provider  # noqa: F401  (fixtures)
from flotta.control.test_mcp_catalogue import _operator
from flotta.control.test_tickets import _as, fleet  # noqa: F401  (fixture)
from flotta.store import FleetStore

URL = "/api/agent-templates"


def _owner(fleet):  # noqa: F811
    return _as(fleet, "ana@example.com")


def test_nothing_is_changed_until_the_operator_changes_it(api, fleet):  # noqa: F811
    assert api.get(URL, headers=_owner(fleet)).json() == {"templates": []}
    assert api.get(f"{URL}/public").json() == {"templates": []}


def test_the_operator_changes_a_template_and_an_agency_reads_it(api, fleet):  # noqa: F811
    saved = api.put(
        f"{URL}/maintenance-developer",
        json={"changes": {"instructions": " You fix {client}'s site. ", "tools": ["notion"]}},
        headers=_operator(),
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["id"] == "maintenance-developer"
    assert body["changes"] == {"instructions": "You fix {client}'s site.", "tools": ["notion"]}
    assert body["updated_at"]
    assert api.get(URL, headers=_owner(fleet)).json() == {"templates": [body]}
    # A member makes agents too.
    assert api.get(URL, headers=_as(fleet, "mo@example.com")).json() == {"templates": [body]}


def test_a_save_replaces_what_was_kept(api, fleet):  # noqa: F811
    api.put(f"{URL}/qa-tester", json={"changes": {"screen": False}}, headers=_operator())
    api.put(f"{URL}/qa-tester", json={"changes": {"offered": False}}, headers=_operator())
    assert api.get(URL, headers=_operator()).json()["templates"][0]["changes"] == {"offered": False}


def test_saving_no_changes_clears_the_template(api, fleet):  # noqa: F811
    api.put(f"{URL}/qa-tester", json={"changes": {"screen": False}}, headers=_operator())
    cleared = api.put(f"{URL}/qa-tester", json={"changes": {}}, headers=_operator())
    assert cleared.json() == {"id": "qa-tester", "changes": {}, "updated_at": None}
    assert api.get(URL, headers=_operator()).json() == {"templates": []}


def test_delete_goes_back_to_what_the_front_end_ships(api, fleet):  # noqa: F811
    api.put(f"{URL}/qa-tester", json={"changes": {"screen": False}}, headers=_operator())
    assert api.delete(f"{URL}/qa-tester", headers=_operator()).json() == {
        "id": "qa-tester",
        "cleared": True,
    }
    assert api.delete(f"{URL}/qa-tester", headers=_operator()).json()["cleared"] is False
    assert api.get(URL, headers=_operator()).json() == {"templates": []}


def test_only_the_operator_changes_a_template(api, fleet):  # noqa: F811
    for headers in (_owner(fleet), _as(fleet, "mo@example.com")):
        refused = api.put(f"{URL}/qa-tester", json={"changes": {"screen": False}}, headers=headers)
        assert refused.status_code == 403, refused.text
        assert api.delete(f"{URL}/qa-tester", headers=headers).status_code == 403
    assert api.put(f"{URL}/qa-tester", json={"changes": {"screen": False}}).status_code == 401
    with FleetStore(fleet) as store:
        assert store.agent_templates() == {}


def test_a_clients_own_people_do_not_read_the_templates(api, fleet):  # noqa: F811
    api.put(f"{URL}/qa-tester", json={"changes": {"instructions": "x"}}, headers=_operator())
    for email in ("vi@example.com", "cu@example.com"):
        refused = api.get(URL, headers=_as(fleet, email))
        assert refused.status_code == 403, refused.text
        assert "instructions" not in refused.text
    assert api.get(URL).status_code == 401


def test_a_change_that_could_not_make_an_agent_is_refused_whole(api, fleet):  # noqa: F811
    for body in (
        {"changes": {"screen": False, "tools": ["no-such-tool"]}},
        {"changes": {"title": "Fixer"}},
        {"changes": []},
        {"screen": False},
        {"changes": {"screen": False}, "more": 1},
    ):
        refused = api.put(f"{URL}/qa-tester", json=body, headers=_operator())
        assert refused.status_code == 422, (body, refused.text)
    assert (
        api.put(
            f"{URL}/Not%20A%20Slug", json={"changes": {"screen": False}}, headers=_operator()
        ).status_code
        == 422
    )
    with FleetStore(fleet) as store:
        assert store.agent_templates() == {}


def test_anyone_may_know_what_is_offered_and_never_the_words(api, fleet):  # noqa: F811
    api.put(
        f"{URL}/qa-tester",
        json={
            "changes": {
                "offered": False,
                "instructions": "SECRET WORDS",
                "schedule": {"cron": "0 9 * * 1", "prompt": "SECRET PROMPT"},
                "rules": {"never": ["SECRET RULE"]},
            }
        },
        headers=_operator(),
    )
    # Words alone are nothing a visitor is told, so that template is not listed.
    api.put(f"{URL}/support", json={"changes": {"instructions": "SECRET"}}, headers=_operator())
    shown = api.get(f"{URL}/public")
    assert shown.status_code == 200
    assert shown.json() == {
        "templates": [
            {
                "id": "qa-tester",
                "changes": {"offered": False, "schedule": True, "rules": {"refused": False}},
            }
        ]
    }
    assert "SECRET" not in shown.text
