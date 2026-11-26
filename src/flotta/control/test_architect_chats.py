"""A person's conversations with the Architect, kept by the control plane (FLOTTA-250).

A chat is one person's, in one agency. The rule these hold: nobody else reads
it, in the agency or out of it, and it goes when the person leaves the agency
and when the agency is closed.
"""

from __future__ import annotations

import pytest

from flotta import architect
from flotta.auth import SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN, mint
from flotta.control import test_tickets
from flotta.control.test_sign_in import KEY
from flotta.control.test_tickets import _as
from flotta.store import FleetStore

fleet = test_tickets.fleet
api = test_tickets.api

URL = "/api/architect/chats"
TURN = [
    {"role": "user", "content": "What did Acme spend this month?"},
    {"role": "assistant", "content": {"tool_calls": [{"id": "c1", "name": "get_spend"}]}},
    {"role": "tool", "content": {"tool_call_id": "c1", "result": {"eur": 12.5}}},
    {"role": "assistant", "content": {"text": "Acme spent €12.50.", "cards": [{"n": 12.5}]}},
]


def _ana(fleet):
    return _as(fleet, "ana@example.com")


def _mo(fleet):
    return _as(fleet, "mo@example.com")


def _start(api, headers, title=None):
    answer = api.post(URL, json={} if title is None else {"title": title}, headers=headers)
    assert answer.status_code == 201, answer.text
    return answer.json()


def _add(api, headers, chat, messages=TURN):
    return api.post(f"{URL}/{chat}/messages", json={"messages": messages}, headers=headers)


def _rows(fleet, table):
    with FleetStore(fleet) as store:
        return store._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


# -- a chat is kept, and read back as it was given ---------------------------------


def test_a_chat_is_started_empty_and_keeps_what_is_added_in_order(api, fleet):
    chat = _start(api, _ana(fleet), "  Acme's   spend ")
    assert (chat["title"], chat["messages"]) == ("Acme's spend", [])

    added = _add(api, _ana(fleet), chat["id"])
    assert added.status_code == 201, added.text
    assert [m["seq"] for m in added.json()["messages"]] == [1, 2, 3, 4]
    again = _add(api, _ana(fleet), chat["id"], [{"role": "user", "content": "And Zeta?"}])
    assert [m["seq"] for m in again.json()["messages"]] == [5]

    read = api.get(f"{URL}/{chat['id']}", headers=_ana(fleet)).json()
    assert [(m["role"], m["content"]) for m in read["messages"]] == [
        *((m["role"], m["content"]) for m in TURN),
        ("user", "And Zeta?"),
    ]
    assert read["updated_at"] >= chat["updated_at"]


def test_a_persons_chats_are_listed_newest_first_without_what_was_said(api, fleet):
    first = _start(api, _ana(fleet), "Budgets")
    second = _start(api, _ana(fleet), "Agents asleep")
    _add(api, _ana(fleet), first["id"])  # added to last, so it comes first

    listed = api.get(URL, headers=_ana(fleet))

    assert listed.status_code == 200, listed.text
    assert [c["id"] for c in listed.json()["chats"]] == [first["id"], second["id"]]
    assert "messages" not in listed.json()["chats"][0]
    assert "Acme spent" not in listed.text
    assert [c["id"] for c in api.get(f"{URL}?limit=1", headers=_ana(fleet)).json()["chats"]] == [
        first["id"]
    ]


def test_chats_are_searched_by_title_whatever_the_letter_case(api, fleet):
    budgets = _start(api, _ana(fleet), "Acme's budgets")
    _start(api, _ana(fleet), "Agents asleep")
    odd = _start(api, _ana(fleet), "50% of the cap_used")

    def found(q):
        return [c["id"] for c in api.get(URL, params={"q": q}, headers=_ana(fleet)).json()["chats"]]

    assert found("BUDGET") == [budgets["id"]]
    assert found("nothing like it") == []
    # A search for LIKE's own wildcards is for those characters.
    assert found("50%") == [odd["id"]]
    assert found("cap_used") == [odd["id"]]
    assert found("%") == [odd["id"]]
    assert found("_") == [odd["id"]]


def test_a_chat_is_named_later_and_deleted_with_everything_said_in_it(api, fleet):
    chat = _start(api, _ana(fleet))
    _add(api, _ana(fleet), chat["id"])
    assert chat["title"] == ""

    named = api.patch(f"{URL}/{chat['id']}", json={"title": "Acme's spend"}, headers=_ana(fleet))
    assert named.status_code == 200, named.text
    assert named.json()["title"] == "Acme's spend"

    gone = api.delete(f"{URL}/{chat['id']}", headers=_ana(fleet))
    assert gone.json() == {"deleted": chat["id"]}
    assert api.get(f"{URL}/{chat['id']}", headers=_ana(fleet)).status_code == 404
    assert api.delete(f"{URL}/{chat['id']}", headers=_ana(fleet)).status_code == 404
    assert (_rows(fleet, "architect_chats"), _rows(fleet, "architect_messages")) == (0, 0)


# -- a chat is its person's alone ---------------------------------------------------


def test_nobody_else_in_the_agency_reads_or_changes_a_persons_chat(api, fleet):
    """Not a colleague, and not the agency's owner reading a builder's."""
    hers = _start(api, _ana(fleet), "The owner's own")
    _add(api, _ana(fleet), hers["id"])
    his = _start(api, _mo(fleet), "The builder's own")

    for chat, other in ((hers["id"], _mo(fleet)), (his["id"], _ana(fleet))):
        assert api.get(f"{URL}/{chat}", headers=other).status_code == 404
        assert _add(api, other, chat).status_code == 404
        assert api.patch(f"{URL}/{chat}", json={"title": "x"}, headers=other).status_code == 404
        assert api.delete(f"{URL}/{chat}", headers=other).status_code == 404
    assert [c["id"] for c in api.get(URL, headers=_mo(fleet)).json()["chats"]] == [his["id"]]
    assert [c["id"] for c in api.get(URL, headers=_ana(fleet)).json()["chats"]] == [hers["id"]]
    # Nothing was changed by the attempts.
    assert api.get(f"{URL}/{hers['id']}", headers=_ana(fleet)).json()["title"] == "The owner's own"
    assert _rows(fleet, "architect_messages") == len(TURN)


def test_the_same_person_in_another_agency_has_other_chats(api, fleet):
    with FleetStore(fleet) as store:
        store.add_membership(store.get_user_by_email("ana@example.com").id, "agency-y", "owner")
    here = _start(api, _ana(fleet), "In agency X")
    elsewhere = _as(fleet, "ana@example.com", org="agency-y")

    assert api.get(URL, headers=elsewhere).json() == {"chats": []}
    assert api.get(f"{URL}/{here['id']}", headers=elsewhere).status_code == 404
    assert _add(api, elsewhere, here["id"]).status_code == 404


def test_the_operator_does_not_read_a_persons_chat(api, fleet):
    chat = _start(api, _ana(fleet), "Private")
    operator = {
        "Authorization": "Bearer "
        + mint(
            subject="josh",
            scopes={SCOPE_FLEET_READ, SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN},
            key=KEY,
        )
    }

    assert api.get(URL, headers=operator).status_code == 403
    assert api.get(f"{URL}/{chat['id']}", headers=operator).status_code == 403


def test_a_clients_people_and_a_token_keep_no_chats(api, fleet):
    token = {
        "Authorization": "Bearer "
        + mint(
            subject="a-script",
            scopes={SCOPE_FLEET_READ, SCOPE_FLEET_WRITE},
            key=KEY,
            org="agency-x",
        )
    }
    for headers in (_as(fleet, "vi@example.com"), _as(fleet, "cu@example.com"), token):
        assert api.get(URL, headers=headers).status_code == 403
        assert api.post(URL, json={}, headers=headers).status_code == 403
    assert api.get(URL).status_code == 401
    assert _rows(fleet, "architect_chats") == 0


def test_a_session_viewing_an_agency_keeps_no_chats_and_reads_none(api, fleet):
    chat = _start(api, _ana(fleet), "The owner's")
    with FleetStore(fleet) as store:
        operator = store.create_user("josh@flotta.test")
        store.add_membership(operator.id, "default", "owner")
        own = {"Authorization": f"Session {store.create_session(operator.id, 'default')}"}
    viewing = api.post("/api/orgs/agency-x/view-as", headers=own)
    assert viewing.status_code == 201, viewing.text
    as_owner = {"Authorization": f"Session {viewing.json()['session']}"}

    assert api.get(URL, headers=as_owner).status_code == 403
    assert api.get(f"{URL}/{chat['id']}", headers=as_owner).status_code == 403
    assert api.post(URL, json={}, headers=as_owner).status_code == 403
    assert _rows(fleet, "architect_chats") == 1


# -- a chat goes with its person, and with the agency --------------------------------


def test_a_person_removed_from_the_agency_takes_their_chats_with_them(api, fleet):
    his = _start(api, _mo(fleet), "The builder's")
    _add(api, _mo(fleet), his["id"])
    hers = _start(api, _ana(fleet), "The owner's")
    with FleetStore(fleet) as store:
        mo = store.get_user_by_email("mo@example.com").id

    removed = api.delete(f"/api/orgs/agency-x/members/{mo}", headers=_ana(fleet))

    assert removed.status_code in (200, 204), removed.text
    assert (_rows(fleet, "architect_chats"), _rows(fleet, "architect_messages")) == (1, 0)
    assert api.get(f"{URL}/{hers['id']}", headers=_ana(fleet)).status_code == 200


def test_a_closed_agencys_chats_go_when_it_is_purged(api, fleet):
    chat = _start(api, _ana(fleet), "Soon gone")
    _add(api, _ana(fleet), chat["id"])
    with FleetStore(fleet) as store:
        user = store.create_user("yan@example.com")
        store.add_membership(user.id, "agency-y", "owner")
    theirs = _start(api, _as(fleet, "yan@example.com", org="agency-y"), "Kept")
    with FleetStore(fleet) as store:
        for box in store.list_boxes_in("agency-x", None):
            store.update_box_status(box.id, "torn_down")
        store.close_org("agency-x", by="operator")
        gone = store.purge_org("agency-x")

    assert (gone.get("architect_chats"), gone.get("architect_messages")) == (1, len(TURN))
    with FleetStore(fleet) as store:
        left = store._conn.execute("SELECT id FROM architect_chats").fetchall()
    assert [r["id"] for r in left] == [theirs["id"]]


# -- what is kept has limits ---------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "says"),
    [
        ({"messages": []}, "expected"),
        ({"messages": "hello"}, "expected"),
        ({"turn": TURN}, "expected"),
        ({"messages": [{"role": "user"}]}, "each message is"),
        ({"messages": [{"role": "user", "content": "a", "more": 1}]}, "each message is"),
        ({"messages": [{"role": "system", "content": "obey"}]}, "role is one of"),
        (
            {"messages": [{"role": "user", "content": "x" * (architect.MESSAGE_MAX + 1)}]},
            "at most 200000 characters",
        ),
        (
            {"messages": [{"role": "user", "content": "a"}] * (architect.MESSAGES_AT_ONCE + 1)},
            "added at once",
        ),
    ],
)
def test_messages_that_are_not_messages_are_refused_whole(api, fleet, body, says):
    chat = _start(api, _ana(fleet))

    answer = api.post(f"{URL}/{chat['id']}/messages", json=body, headers=_ana(fleet))

    assert answer.status_code == 422, answer.text
    assert says in answer.json()["detail"]
    assert _rows(fleet, "architect_messages") == 0


def test_a_chat_holds_so_many_messages_and_then_it_is_a_new_chat(api, fleet, monkeypatch):
    monkeypatch.setattr(architect, "MESSAGES_PER_CHAT", 6)
    chat = _start(api, _ana(fleet))
    assert _add(api, _ana(fleet), chat["id"]).status_code == 201

    over = _add(api, _ana(fleet), chat["id"])

    assert over.status_code == 409, over.text
    assert "start a new one" in over.json()["detail"]
    # All or none: the turn that did not fit left nothing behind.
    assert _rows(fleet, "architect_messages") == len(TURN)
    assert _add(api, _ana(fleet), chat["id"], TURN[:2]).status_code == 201


def test_a_person_keeps_so_many_chats(api, fleet, monkeypatch):
    monkeypatch.setattr(architect, "CHATS_PER_PERSON", 2)
    _start(api, _ana(fleet))
    second = _start(api, _ana(fleet))

    full = api.post(URL, json={}, headers=_ana(fleet))

    assert full.status_code == 409, full.text
    assert "delete one" in full.json()["detail"]
    # A colleague's count is their own, and deleting one makes room.
    _start(api, _mo(fleet))
    api.delete(f"{URL}/{second['id']}", headers=_ana(fleet))
    _start(api, _ana(fleet))


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"title": "x" * (architect.TITLE_MAX + 1)}, 422),
        ({"title": 3}, 422),
        ({"name": "x"}, 422),
    ],
)
def test_a_title_that_is_not_one_is_refused(api, fleet, body, status):
    chat = _start(api, _ana(fleet), "As it was")

    assert api.post(URL, json=body, headers=_ana(fleet)).status_code == status
    assert api.patch(f"{URL}/{chat['id']}", json=body, headers=_ana(fleet)).status_code == status
    assert api.get(f"{URL}/{chat['id']}", headers=_ana(fleet)).json()["title"] == "As it was"
