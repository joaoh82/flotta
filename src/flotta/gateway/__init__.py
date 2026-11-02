"""The model gateway (M13.2): every model call a box makes, metered.

A box points its model client at this and authenticates with its own token. The
gateway swaps that for the provider key, streams the reply back, and writes one
priced `usage` row per call. See `flotta.gateway.app`.
"""

from flotta.gateway.app import create_gateway

__all__ = ["create_gateway"]
