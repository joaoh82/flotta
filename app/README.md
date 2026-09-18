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

## Watching an agent work

A turn is rarely one model call. Asked what it can reach, an agent once ran
seven shell commands over forty seconds, and the window said "thinking…" for
all of it — a turn doing real work looked exactly like one that had hung.

Hermes reports every step as it goes, and the conversation now shows them:

- **Each tool as it runs**, with what it was asked to do (`terminal  ls
  /workspace`), then a tick or a cross and how long it took. A command that
  ran and exited non-zero is a cross — that eng-r turn had three, and each one
  used to look like progress.
- **What the agent said before using a tool** ("Checking the machine now.") as
  its own line. The final reply does not repeat it — measured on a live box —
  so it would otherwise vanish.
- **The answer as it is written**, replaced by the finished reply.
- **The tail of its reasoning**, faint, while there is nothing else to show,
  and one line saying what it is doing now: preparing a tool, running one,
  writing, or reasoning.

Switching to another agent and back mid-turn shows the turn so far again
rather than a blank pane. The steps are the window's, not the agent's memory:
reopening a finished conversation shows what was said, not which commands ran.

## Replies are Markdown, with three things refused

Agents answer in Markdown nearly every turn, so their words render as it:
tables, code blocks, lists, headings. Your own messages and Flotta's errors
stay plain text.

What an agent writes is untrusted — it reads repositories, pages and command
output, and any of them can put text in a reply — so three things Markdown
would normally do are refused:

- **HTML is shown as text**, never turned into elements.
- **Images are not loaded.** Loading one would mean the window fetching a URL
  an agent chose, which is the one thing the app is built never to do. The
  alt text is shown instead.
- **Links do not open.** The window has no permission to open a browser, on
  purpose, so a clickable link would replace the app itself with the page. A
  link shows its text and address, so you can copy it.

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

You do not have to have the conversation open when you save. `attach` compares
when the instructions were last written against the session it was about to
resume, and starts a fresh one instead when the instructions are newer.

**That comparison re-reads the timestamp every time it attaches**, including on
a resync. Reading it once when the conversation task started looked like an
obvious saving and was the bug: saving from the Info panel unmounts the
transcript, so coming back runs a resync, and a resync holding a timestamp from
before the edit resumed the old session and kept the old persona with nothing
on screen to say why. Both timestamps are epoch seconds: the box's own `started_at`, and
the control plane's, from the `instructions_changed` event.

Clearing the field removes the file rather than leaving an empty one, so the
agent falls back to Hermes's own default identity instead of having none.

## Repositories, and what a grant actually is

The Info panel grants and revokes the repositories an agent may clone, commit
to and push. The capability shipped long before the panel: a box has held a git
credential helper since FLOTTA-20, asking the control plane per repository and
per invocation, holding no GitHub credential of its own.

Paste a slug, an https URL or an ssh URL — the control plane decides what it
meant, and the list comes back normalised, so what is displayed is what was
granted rather than what was typed.

**The boundary runs in both directions, and the panel says so.**

*Flotta narrows.* The source is one fleet token, so a box that extracted a
credential could reach repositories it was never granted. Flotta refuses to
hand one over for an ungranted repository; GitHub does not enforce the
narrowing. A tidy list of granted repositories reads like enforcement, and a
soft boundary that reads as a hard one is worse than no boundary.

*Flotta cannot widen.* Granting a repository the fleet token has no access to
used to be accepted and then failed on the agent, as GitHub's `Write access to
repository not granted` — which reads as a write problem during a clone and
sends you to the wrong settings page. The control plane now asks GitHub before
storing the grant and refuses with the reason that is actually true. It refuses
only on a definite no: a GitHub outage still lets you configure your fleet,
because the check removes a surprise rather than acting as a gate.

FLOTTA-22 closes the first half properly, with GitHub App installation tokens
scoped to `repository_ids`, and that is the day this section gets to shrink.

Verified live on a private repository, on one running machine with no restart:
ungranted clones fail with a 403 from the credential helper, granting makes the
same clone succeed, and revoking makes the next one fail again.

## Colleagues: agents asking each other (M7)

**Every agent may ask every other agent** (FLOTTA-65). A new agent is on
everyone's list the moment it is running; nothing has to be granted.

An agent's **Info** panel has a **Colleagues** section: everyone it may ask,
each with **Block**, the agents it has been blocked from asking, each with
**Allow**, and the recent conversations between them.

- **Blocks are one way.** Blocking eng-g in eng-r's panel stops eng-r asking
  eng-g; eng-g may still ask eng-r.
- **The agent never holds a way to reach another agent.** It runs
  `flotta-ask`, which asks the control plane to carry the message; the control
  plane checks the list, wakes the other agent and brings the reply back. An
  agent cannot change its own list.
- **Flotta stops loops and runaways.** A question cannot come back to an agent
  already in the chain, cannot be passed along more than three times, and one
  agent's questions are capped over a few minutes. A stopped message shows up
  in *Recent conversations* as "stopped by Flotta", with the reason.

**The name "Colleagues" is load-bearing.** Refusals from the control plane and
the skill on every box tell the agent to send the person to "this agent's Info
panel, Colleagues". Rename the section and those sentences lie.

While an agent is asking, the live step list shows **asking eng-r** and the
question instead of the terminal command (`colleagues.ts`'s `delegationOf`).
The answering agent holds the question in a **separate session**, so it never
appears in the transcript a person has with it — *Recent conversations* is the
only place eng-r's side is visible.

### `@` in a chat

Typing `@` in any agent's chat offers its colleagues — by address, or by what
they are called (`@rev` finds the reviewer). The list is the control plane's,
so a blocked agent or one still being built is never offered.

`@eng-g` means nothing to a model on its own, so a message that names
colleagues is sent with a short **Flotta note** after it: who each one is, and
the `flotta-ask` command. The note says nothing about *whether* to ask — the
person's words do that.

**The note is hidden wherever a person's message is shown**, not only when it
is sent: a resumed conversation's history comes back from Hermes with the note
still attached (`mentions.ts`'s `withoutNote`). Anything from the note's
opening marker to the end of a message is hidden, which is why the marker is
one nobody types.

## A model per agent (FLOTTA-39)

**Info → Model** shows what an agent runs and whose choice that is — its own,
or the fleet's — with **Change**. The Create form has a *Model* field under
"Give it a different model, size or region", with the fleet's model as the
placeholder — read from **Settings → Fleet**, not from another agent's row, so
it is right on the first create when there are no rows to read.

**Settings → Fleet** carries *Model for new agents*, *Provider endpoint*, and
*Projects folder* (`FLOTTA_WORKDIR`, default `/workspace`). The endpoint is
shown and **not editable**: it decides where the fleet's API key is sent, so a
settable one would turn `fleet:write` into a way to have that key delivered
somewhere else. Changing the model or the projects folder there affects agents
created afterwards; existing ones keep what they were made with (FLOTTA-66,
FLOTTA-56).

The projects folder is on the machine's disk, not the memory volume. **An image
update replaces that disk**, so clones are lost; the Info panel says so, and
the agent is told to re-clone if the folder is empty. A second volume for
projects is deferred until agents do multi-day work with uncommitted state
(M6).

- Changing a model restarts a running agent (about a minute) and leaves a
  sleeping one asleep until it wakes. Memory and conversations are kept.
- A model id is checked against OpenRouter's catalogue first, so a typo is
  refused in the form rather than surfacing as "model not found" on every turn.
- Only the model is per agent. The endpoint and key stay the fleet's.

**Identity → Renew** sits on the row below, and the two behave the same way:
both write one value to the agent's own machine through the control plane.

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

**Projects folder** is on this panel because a folder that silently vanishes
is worse than one that is not configurable. The path is what the machine has
(`FLOTTA_WORKDIR` in its env), falling back to `/workspace` for agents created
before the setting existed. The lifetime is stated in the same breath: clones
do not survive an update. The **Update agents** confirmation says the same
thing, because a button that wipes checkouts without saying so is the window
telling a lie by omission.

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
