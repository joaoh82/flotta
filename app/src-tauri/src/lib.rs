//! The Flotta app (M8).
//!
//! A list of agents from the control plane, and — from M8.2 — a conversation
//! per agent straight to that box through the door. Hermes is the engine
//! inside the box; this is the client, and it is ours.
//!
//! Everything that touches the network or the keychain is in `fleet`. The
//! commands here are a thin boundary: they exist so the webview can ask for
//! work without ever holding a credential.

mod agent;
mod fleet;

use agent::Conversations;
use fleet::{BoxRow, FleetError, Settings};
use std::fs;
use std::path::PathBuf;
use tauri::Manager;
use tokio::sync::mpsc;

fn settings_path(app: &tauri::AppHandle) -> Result<PathBuf, FleetError> {
    let dir = app
        .path()
        .app_config_dir()
        .map_err(|e| FleetError::Unexpected(format!("no config directory: {e}")))?;
    fs::create_dir_all(&dir)
        .map_err(|e| FleetError::Unexpected(format!("cannot create {}: {e}", dir.display())))?;
    Ok(dir.join("settings.json"))
}

fn read_settings(app: &tauri::AppHandle) -> Settings {
    // A missing or unreadable file is "not configured yet", not an error: the
    // first run has no file, and that is the normal path, not a failure.
    settings_path(app)
        .ok()
        .and_then(|p| fs::read_to_string(p).ok())
        .and_then(|raw| serde_json::from_str(&raw).ok())
        .unwrap_or_default()
}

/// What the settings screen shows. **Never the token** — the app can say
/// whether one is stored, which is all the UI needs to render, and returning
/// it would hand the webview the one thing this design keeps from it.
#[derive(serde::Serialize)]
struct SettingsView {
    control_url: String,
    domain: String,
    has_token: bool,
    /// Set when the keychain could not be *read*, which is a different thing
    /// from there being no token in it — and the difference is the whole bug
    /// this field exists to end. See `load_settings`.
    token_error: Option<String>,
}

#[tauri::command]
fn load_settings(app: tauri::AppHandle) -> SettingsView {
    let settings = read_settings(&app);

    // This was `matches!(read_token(), Ok(Some(_)))`, which folded a keychain
    // *failure* into "no token saved" — and on macOS that failure is routine
    // rather than exotic: a rebuilt binary has a different signature, so the
    // ACL on the existing item no longer matches it and the read is denied.
    // Every `just app` during development can do it.
    //
    // The app then opened its settings screen saying "None saved yet", the
    // token was right there in the keychain, and closing that screen left
    // "No agents yet" on a fleet that had one. Three layers, each honest on
    // its own, and a conclusion that was false at every step.
    let (has_token, token_error) = match fleet::read_token() {
        Ok(token) => (token.is_some(), None),
        Err(err) => (false, Some(err.detail().to_string())),
    };

    SettingsView {
        control_url: settings.control_url,
        domain: settings.domain,
        has_token,
        token_error,
    }
}

/// Save settings. `token` is optional: `None` leaves the stored one alone, so
/// editing a URL does not require pasting a token again. An empty string is a
/// deliberate clear.
#[tauri::command]
fn save_settings(
    app: tauri::AppHandle,
    control_url: String,
    domain: String,
    token: Option<String>,
) -> Result<(), FleetError> {
    let control_url = control_url.trim().to_string();

    // Validated here, not just guarded at render. A scheme-less paste —
    // `my-fleet.up.railway.app` — used to save cleanly and then throw in the
    // header on every launch, *including* with settings open, because the
    // header sits above the form. The only recovery was hand-editing
    // settings.json, which is not a recovery a person will find.
    //
    // Both halves are fixed: this refuses to store one, and the header no
    // longer trusts what it reads. Either alone would leave a way in — this
    // one cannot help a settings.json that is already wrong, and the render
    // guard alone would keep saving values that cannot work.
    // Empty is allowed and means "not configured yet" — the app has a state for
    // that. Anything else has to be absolute, because that is what `new URL`
    // and `reqwest` both require.
    let unset_or_absolute = control_url.is_empty()
        || control_url.starts_with("http://")
        || control_url.starts_with("https://");
    if !unset_or_absolute {
        return Err(FleetError::NotConfigured(format!(
            "{control_url:?} is not a URL — it needs a scheme. Try https://{control_url}"
        )));
    }

    // The domain gets the same treatment, for the same reason and one it
    // learned the hard way: the first person to test the URL validation pasted
    // the test value into *this* field instead, and it was stored without a
    // murmur. A hostname is not a URL — `<box>.<domain>` is built from it — so
    // a scheme here is as wrong as no scheme there, and silently accepting
    // either produces an address that cannot resolve at a point (M8.2) far
    // from where it was typed.
    let domain = domain.trim().trim_end_matches('/').to_string();
    if domain.contains("://") || domain.contains('/') {
        return Err(FleetError::NotConfigured(format!(
            "{domain:?} is not a domain — agents are reached at <name>.<domain>,              so it wants something like flotta.dev"
        )));
    }

    let settings = Settings {
        control_url,
        domain,
    };
    let path = settings_path(&app)?;
    fs::write(
        &path,
        serde_json::to_string_pretty(&settings)
            .map_err(|e| FleetError::Unexpected(e.to_string()))?,
    )
    .map_err(|e| FleetError::Unexpected(format!("cannot write {}: {e}", path.display())))?;

    if let Some(token) = token {
        fleet::write_token(token.trim())?;
    }
    Ok(())
}

#[tauri::command]
async fn list_boxes(app: tauri::AppHandle) -> Result<Vec<BoxRow>, FleetError> {
    fleet::list_boxes(&read_settings(&app)).await
}

#[tauri::command]
async fn create_agent(app: tauri::AppHandle, name: String) -> Result<BoxRow, FleetError> {
    fleet::create_box(&read_settings(&app), &name).await
}

/// Destroy an agent. `confirm` must be the agent's own name.
///
/// The confirmation is checked here, not only in the UI, because this is the
/// one verb that deletes an agent's entire memory — months of it — and a
/// dialog is a thing people click through. Typing the name is a deliberate
/// act; clicking "OK" is a reflex.
#[tauri::command]
async fn destroy_agent(
    app: tauri::AppHandle,
    state: tauri::State<'_, Conversations>,
    id: String,
    name: String,
    confirm: String,
) -> Result<(), FleetError> {
    if confirm.trim() != name.trim() {
        return Err(FleetError::Unexpected(format!(
            "type {name} to confirm — this deletes everything it remembers"
        )));
    }
    // Close the conversation first: a socket to a machine being destroyed
    // fails in a way that reads like a network problem.
    state.forget(&name);
    fleet::destroy_box(&read_settings(&app), &id).await
}

#[tauri::command]
async fn open_conversation(
    app: tauri::AppHandle,
    state: tauri::State<'_, Conversations>,
    box_name: String,
) -> Result<(), FleetError> {
    // Selecting an agent you are already talking to must not open a second
    // socket — the box would see two sessions and one's reply would arrive on
    // the other. But "already open" has to mean *live*: a finished task leaves
    // a sender nobody reads, and returning early against one made the agent
    // unreachable until the app restarted.
    if state.live(&box_name).is_some() {
        // The UI resets to "waking" on mount, so it needs telling that the
        // connection it is waiting for already exists.
        agent::announce_ready(&app, &box_name);
        return Ok(());
    }

    let settings = read_settings(&app);
    let Some(token) = fleet::read_token()? else {
        return Err(FleetError::NotConfigured(
            "No access token — set one in Settings before talking to an agent.".into(),
        ));
    };

    // A small buffer, not zero: a person can type a second message while the
    // first is in flight, and the alternative is the UI blocking on send.
    let (tx, rx) = mpsc::channel(8);
    state.insert(box_name.clone(), tx);

    tauri::async_runtime::spawn(agent::run(app, settings, box_name, token, rx));
    Ok(())
}

#[tauri::command]
async fn send_prompt(
    state: tauri::State<'_, Conversations>,
    box_name: String,
    text: String,
) -> Result<(), FleetError> {
    let Some(sender) = state.live(&box_name) else {
        return Err(FleetError::Unexpected(format!(
            "the conversation with {box_name} is not open"
        )));
    };
    sender
        .send(text)
        .await
        .map_err(|_| FleetError::Unreachable(format!("the conversation with {box_name} has ended")))
}

#[tauri::command]
fn close_conversation(state: tauri::State<'_, Conversations>, box_name: String) {
    // Dropping the sender ends the task's receive loop, which closes the
    // socket. No explicit shutdown message needed, and none that could be
    // missed.
    state.forget(&box_name);
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(Conversations::default())
        .invoke_handler(tauri::generate_handler![
            load_settings,
            save_settings,
            list_boxes,
            open_conversation,
            send_prompt,
            close_conversation,
            create_agent,
            destroy_agent
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
