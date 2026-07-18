"""Flotta worker package: the Modal image + headless-Hermes container entrypoint.

Import discipline: this package boots inside a Modal container where `modal`,
`mcp`, and the Hermes Agent (`run_agent.AIAgent`) are installed. None of those
are dependencies of the base `flotta` package, so every heavy import in this
package is kept lazy (inside the function that needs it). The import-light
cores — `config.WorkerConfig`, `server.authorize`, `server._run_task_core` —
stay unit-testable with the standard library alone.
"""
