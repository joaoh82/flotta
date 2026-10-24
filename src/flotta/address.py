"""A box's address: one DNS label naming the box, its client and its org (M11).

    eng-d                     the default org's default client — every box
                              made before tenancy, and the operator's own
    support--acme--agency-x   `support`, of client `acme`, of org `agency-x`

**One label, not three.** `support.acme.agency-x.flotta.dev` reads better, but
the `*.flotta.dev` record and its certificate cover exactly one label, and a
wildcard certificate per client is infrastructure nobody has asked for. So the
parts are joined with `--`, which is why no box name, client id or org id may
contain it (`store.validate_box_name`, `store.validate_tenant_id`).

**The label is the box's name.** `boxes.name` stores it whole, so it stays
unique across the store — and that is load-bearing, not tidy: the Fly app is
named after it, and `FlyBackend.create` *adopts* a machine already in that app.
Two orgs' `support` agents sharing a name would share a machine.

**One address per box.** The default org's default client is the bare name
and nothing else; `eng-d--default--default` is refused rather than accepted as
a second spelling, so the door has exactly one string to compare.

Shared by the store, which composes the label when it creates a box, and the
front door, which parses one out of a hostname — the door has no store, and
two copies of the rule would drift.
"""

from __future__ import annotations

from flotta.auth import DEFAULT_CLIENT, DEFAULT_ORG

SEPARATOR = "--"

#: One DNS label.
LABEL_MAX = 63


def compose(name: str, *, org: str, client: str) -> str:
    """The label a box called `name` in `org`'s `client` is reached at.

    Does not validate the parts; the store does, before calling this.
    """
    if org == DEFAULT_ORG and client == DEFAULT_CLIENT:
        return name
    return SEPARATOR.join((name, client, org))


def parse(label: str) -> tuple[str, str, str]:
    """`(name, client, org)` from a label, or `ValueError` if it is not one."""
    parts = label.split(SEPARATOR)
    if len(parts) == 1:
        name, client, org = parts[0], DEFAULT_CLIENT, DEFAULT_ORG
    elif len(parts) == 3:
        name, client, org = parts
    else:
        raise ValueError(f"{label!r} is not an address: it is `<box>--<client>--<org>`")
    if not all(parts):
        raise ValueError(f"{label!r} is not an address: a part is empty")
    if compose(name, org=org, client=client) != label:
        raise ValueError(f"{label!r} is not an address: the default org's boxes are bare")
    return name, client, org
