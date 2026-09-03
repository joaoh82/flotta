# The Flotta app

The desktop client. A list of your agents, and — from M8.2 — a conversation
with each one.

Run it with `just app` from the repo root.

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
  shipped app cannot be made to take a token from the environment.
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
