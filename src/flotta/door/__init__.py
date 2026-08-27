"""The front door — public, authenticated access to a box (M5b).

`<box>.flotta.dev` terminates TLS here and proxies to that box's
`hermes serve` over Fly's private network, so reaching an agent stops
requiring `flyctl` and a WireGuard tunnel. That is what makes the Flotta app
(M8) shippable to anyone.
"""

from flotta.door.app import BoxUnavailable, create_door, host_to_box_name

__all__ = ["BoxUnavailable", "create_door", "host_to_box_name"]
