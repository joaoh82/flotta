# The Flotta app

The desktop client. A list of your agents, and — from M8.2 — a conversation
with each one.

Run it with `just app` from the repo root. With `$FLOTTA_SIGNING_KEY` in
`.env` that recipe mints its own one-day token, so there is nothing to paste
— see *the keychain and rebuilt binaries* below for why development needs one
in the environment at all.

## Why a desktop app and not a page

Not packaging. Two things a browser cannot do without giving something up:

- **CORS.** A page served at `localhost` calling `https://<fleet>.up.railway.app`
  needs cross-origin headers on every endpoint of the control plane, and on the
  door's WebSocket upgrade. Requests made from Rust are not browser requests,
  so none of that exists.
- **Where the token lives.** In a browser it would be in `localStorage`,
  readable by anything that gets script execution in the page — including a
  rendered agent reply. Here it is in the OS keychain, and the webview never
  receives it: `load_settings` reports *whether* a token is stored and never
  what it is.

**The rule that keeps this true: no URL of ours is ever fetched from the
frontend.** Every request crosses the Tauri boundary into `src-tauri/src/fleet.rs`.
If that stops being true, this is a browser page with an installer and should
have been one.

## Layout

| | |
|---|---|
| `src/` | React + Tailwind. Renders; never fetches. |
| `src-tauri/src/fleet.rs` | The keychain and every outbound request. |
| `src-tauri/src/lib.rs` | The commands the webview may call. |

## Configuration

On first run the app opens its settings. It needs:

- **Control plane URL** — where the fleet API runs.
- **Box domain** — agents are reached at `<name>.<domain>`.
- **Access token** — mint one with
  `flotta token mint you --scope fleet:read --scope box:chat`.

The URL and domain go in a JSON file under the app's config directory; the
token goes in the keychain. On macOS you can confirm that:

```sh
security find-generic-password -s dev.flotta.app -a control-plane-token
```

## Start over, and why a conversation has a boundary at all

Hermes renders an agent's system prompt **once, when a session starts**, and
stores it on the agent's volume keyed by hash. An agent's standing
instructions (`$HERMES_HOME/SOUL.md`) are read at that moment and never again
for the life of that session. The freeze is deliberate — the prompt is built in
cache tiers so a provider can reuse the longest unchanged prefix, and
re-rendering the identity every turn would re-bill the whole thing.

So changing what an agent *is* needs a new session, and **Start over** in the
conversation header is how the window asks for one. It is the only way: there
is no "reload the instructions" call, and rebuilding the machine does not help,
because the frozen prompt lives on the volume that a re-image preserves.

Nothing is destroyed. The agent's memory is under `HERMES_HOME`, not inside the
conversation, so it keeps what it learned and loses only the visible
back-and-forth. The previous transcript stays on the volume; the window does
not offer a way back to it, because `session.most_recent` answers with the
newest and **Flotta deliberately has no concept of a session list**. A person
here has one conversation with each agent, and Hermes's vocabulary stays inside
Hermes.

Found the hard way: an agent created before instruction-seeding shipped kept
introducing itself as stock Hermes through a re-image and two fleet updates
(FLOTTA-58).

### Editing standing instructions

The Info panel's **Standing instructions** section is editable, and saving does
two things that have to happen together: it writes `SOUL.md` on the agent's
volume, and it starts a fresh conversation. Saving without the restart would
change nothing the agent says, so the window does not offer that half on its
own — the button says what it does.

If the conversation is not open when you save, nothing extra is needed.
`attach` compares when the instructions were last written against the session
it was about to resume, and starts a fresh one instead when the instructions
are newer. Both timestamps are epoch seconds: the box's own `started_at`, and
the control plane's, from the `instructions_changed` event.

Clearing the field removes the file rather than leaving an empty one, so the
agent falls back to Hermes's own default identity instead of having none.

## Info: the store's belief, and the substrate's answer

Every read in this window comes from the fleet store, which is a *belief* — a
row written by whatever last reached the machine. The list is right to use it:
it is cheap, and every verb that touches a box updates it. But it means the app
could not say what image an agent runs, how big its disk is, or whether the row
was even still true.

`⋯ → Info` asks the substrate (`GET /api/boxes/{id}/machine`) and shows the
answer **beside** the row rather than instead of it. When they disagree — a row
saying `running` about a machine Fly stopped during a host drain, an agent
woken through the door before the row caught up — the panel says so. Merging
them into one status would mean silently picking a winner.

It is fetched on open and on Recheck, **never on a timer**: it is a `flyctl`
subprocess on the control plane. The list keeps polling the store, which is
what makes a status change show up without anyone pressing Refresh.

Which Hermes version an image carries is still recorded nowhere — the image tag
is the closest thing until FLOTTA-43.

## Hermes versions, and upgrading from the window

Three facts that are easy to collapse into one, kept apart in the Info panel
because they have never been the same fact:

- **This agent runs** — a label baked into the image it boots
  (`dev.flotta.hermes-ref`, written by `fly/Dockerfile` from its `HERMES_REF`
  build arg). Blank for an image built before that label existed, which is
  every agent in the fleet until it is upgraded; the panel says so rather than
  showing "unknown".
- **Fleet builds** — the pin the *next* image would be built from. Says nothing
  about any running agent.
- **Newest release** — what upstream has. An unreachable GitHub reads as
  unknown, never as up to date.

Moving the pin is `just hermes-bump <tag>`, not a button: it rebuilds the image
and re-runs the live checks, because the headless-boot recipe was validated
against one version and a bump is not mechanical.

What the window *can* do is move an agent onto the image the fleet already
builds. `POST /api/boxes/{id}/upgrade` answers `202` and re-images on a thread
— `flyctl machine update` waits up to 300s and a proxy in front of this cut
`POST /api/boxes` at 60s once already — so the button reports "started" and the
outcome lands in the agent's timeline as `reimaged` or `upgrade_failed`.

It costs a restart, and the confirmation says so. It does **not** cost the
disk: keeping `/data/hermes` is the entire point of an upgrade being a
re-image rather than a rebuild.

## Development: the keychain and rebuilt binaries

An unsigned binary's keychain ACL is tied to that exact binary. Every `cargo`
rebuild therefore produces a binary the existing item does not trust, and macOS
refuses it with:

```
Platform secure storage failure: The user name or passphrase you entered is not correct.
```

That is not a wrong passphrase and not a corrupted keychain — it is the item
declining to talk to a build it has never seen. Release builds, signed with a
stable identity, do not have the problem.

Two things make it survivable:

- **In debug builds only, `$FLOTTA_TOKEN` is read first** — the same variable
  `flotta chat` uses. `cfg` compiles this out of release builds entirely, so a
  shipped app cannot be made to take a token from the environment. `just app`
  sets it for you (`fleet:read`, `fleet:write`, `box:chat`, `box:destroy`, one
  day) when the signing key is present and you have not exported one yourself.
- **Saving a token repairs the item.** `set_password` *updates* in place, which
  needs access the new build does not have; when that fails the app deletes the
  item and creates a fresh one, which needs no such access.

If both somehow fail, remove it by hand and save again:

```sh
security delete-generic-password -s dev.flotta.app -a control-plane-token
```

## What it does not have yet

Conversation is M8.2, and creating or destroying agents is M8.3. There is also
no *user*: the token is minted from the signing key an operator holds, so this
is single-user until M10.
