"""Code that runs **on a box** — Tier 1, the persistent agent.

Distinct from `flotta.worker`, which runs in a disposable Modal container and
is Tier 3. The two boot Hermes from the same recipe and differ in exactly the
things that make a box a box: it keeps its `HERMES_HOME` on a mounted volume,
and it is allowed to remember.
"""
