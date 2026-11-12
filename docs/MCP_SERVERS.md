# MCP connector library

The catalogue itself is `FLOTTA_MCP_SERVERS`. This note is the HQ library on
top of it (`src/flotta/mcp_library.json`): eleven vendor connectors an operator
adds with `POST /api/mcp/library/{id}`. A custom catalogue entry stays possible.
The console that lists these is a separate change.

## What an agency's list shows

`GET /api/orgs/{org}/mcp` adds two things to each server, and changes nothing
that was already there (`name`, `title`, `url`, `auth`, `ask_first`, `tools`,
and `connection`, whose `status` is still `ok` or `broken`).

`library` is true only when the catalogue entry carries `"library": true`, the
marker `catalogue_entry` writes. `library_id` is that entry's manifest id, and
it is present only then. A hand-written server is `library: false` with no
`library_id`, including one that reuses a library name and host. This list does
not use the spec-match fallback that grants do.

`status` is the connection, as an enum:

| `status` | When |
| --- | --- |
| `not_connected` | No connection row |
| `connected` | The gateway would use it (`connection.status` is `ok`) |
| `expired` | The row is broken, and the sealed credential says so: `failure` is `expired`, or there is no refresh token and `expires_at` is already past |
| `error` | Any other broken connection, including a bearer key whose detail happens to mention expiry |

The OpenAPI document for this route carries one example of that object.

## What "added from the library" means

`catalogue_entry` writes a normal catalogue object and sets `"library": true`.
That marker is how a server is recognised. An entry added before the marker
existed is still recognised when its spec is exactly what `catalogue_entry`
would write. A hand-written entry with the same name and host, including the
`linear` example in `flotta.mcp`, is not a library connector, and an omitted
grant on it still means every tool.

## Empty grants

Linear and Make publish no stable tool names (`dynamic_tools`). Their default
grant is empty: nothing is on until the agency names a tool after a
`tools/list`. The library list says so with `tools_enabled: false` and an empty
`read_tools`. Hermes is given `tools.include: []`, a filter of zero tools. An
omitted `tools` key would be no filter, and Hermes would show every tool the
server lists.

## Grants, not OAuth scopes

The per-tool grant is the gate. Read tools are on by default. Write tools are
in the catalogue and off until an agency opts in. A tool this file does not
classify is treated as write. The OAuth consent stays the vendor's own:

| Connector | Scopes |
| --- | --- |
| Notion | `default` |
| Linear | `read`, `write` |
| Airtable | `data.records:read`, `data.records:write`, `schema.bases:read`, `schema.bases:write`, `data.recordComments:read`, `data.recordComments:write`, `workspacesAndBases:read` |
| Calendly | `mcp:scheduling:read`, `mcp:scheduling:write`, `mcp:notetaker:read` |
| Stripe | `mcp` |
| The others | none named |

Read-only consent, a smaller scope set than the vendor's default, is a known
follow-up. It is not a substitute for the grant.

## monday.com

monday stays on dynamic client registration. A monday admin may need to approve
the client Flotta registers. There is no personal-token fallback.

## Intercom

Intercom stays an access token (`auth: api_key`, sent as `Authorization: Bearer`).
Asking Intercom to allowlist a redirect URL is a follow-up, not this change.

## n8n

The template is `https://{host}/mcp-server/http`. The host is a public DNS name.
The port is 443 only. A port in the host parameter is refused at add time, and
again at connect time.

Three tools moved from the default read grant to write, so they stay off until
the agency opts in:

- `list_credentials` lists credential names, types and ids. n8n does not return
  the secret values. The inventory is still opt-in.
- `get_workflow_execution` returns metadata by default and full node output,
  which can hold secrets, when `includeData` is true.
- `explore_node_resources` queries external services with the instance's stored
  credentials (n8n classifies it under `credential:read`).

Left as read, because the vendor's own split says they do not mutate:

- Zapier `execute_zapier_read_action` is the read half. `execute_zapier_write_action` is the write half.
- Atlassian `executeRead` is the read-tier deferred-tool path. `executeWrite` and `executeDestructive` are already write.
- monday `plan_workflow` is the read-only planning step before `create_workflow`.

Notion's names were checked against Notion's MCP tools FAQ
(`https://developers.notion.com/guides/mcp/tools-faqs`). Every name in the
manifest is on that list, including `notion-ai-search`. Newer tools Notion has
added since are not in the allowlist, so they cannot be granted until the
manifest names them.

## Connect-time pin

n8n and Intercom, and any later entry with a URL template, are checked again
when the gateway connects. The host has to still be a public DNS name on port
443. `alerts.check_addresses` then resolves it and requires every address to be
public (the same check budget webhooks use: private, loopback, link-local,
CGNAT, IPv6 ULA, unspecified, multicast, reserved, and IPv4-mapped IPv6).

The request is rewritten with `alerts.pinned_request`, which `alerts.post_pinned`
and `mcp_oauth._send` also use. TCP dials the vetted address. The Host header
is the original hostname. The request extension `sni_hostname` is that hostname
too: httpcore's `AsyncHTTPConnection._connect` passes it to TLS as
`server_hostname`. Redirects are not followed. The MCP client keeps no
keepalive connection, so a TLS session opened for one name is not reused for
another host on the same address. A name such as `127.0.0.1.nip.io` is a legal
DNS name at add time and is refused here when it resolves to loopback.

## Known follow-ups

- **No lock on the catalogue write.** `add_mcp_from_library` reads the current
  list and writes it back (`control/app.py`, the read then `set_settings`).
  Two operators adding at the same moment can lose one add.
- **The environment catalogue freezes into the database on the first add.**
  A stored list replaces the environment. A self-hosted fleet that still
  defines `FLOTTA_MCP_SERVERS` in the environment should know that the first
  library add copies that list into the database, and later environment edits
  no longer apply.
- **A changed manifest entry has no upgrade path.** Adding a connector that is
  already in the catalogue with a different spec returns 409. Changing a
  vendor's URL or tool list in the manifest does not update an entry that was
  already added.
- **Read-only OAuth consent**, as above.
- **Intercom's redirect allowlist**, if Intercom later offers OAuth.
