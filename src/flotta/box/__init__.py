"""Code that runs **on a box** — the persistent agent.

There used to be a second tier here: `flotta.worker`, a disposable Modal
container that booted Hermes from the same recipe but was not allowed to
remember. It was cut with the shard tier — once a box is an agent you can
wake, talk to and delegate between, a tier with no memory earns nothing.

What makes a box a box is what that comparison used to highlight: it keeps its
`HERMES_HOME` on a mounted volume, and it is allowed to remember.
"""
