//! The Flotta app (M8).
//!
//! A list of agents from the control plane, and — from M8.2 — a conversation
//! per agent straight to that box through the door. Hermes is the engine
//! inside the box; this is the client, and it is ours.
//!
//! Everything that touches the network or the keychain is in `fleet`. The
//! commands here are a thin boundary: they exist so the webview can ask for
//! work without ever holding a credential.

mod fleet;

use fleet::{BoxRow, FleetError, Settings};
use std::fs;
use std::path::PathBuf;
use tauri::Manager;

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
}

#[tauri::command]
fn load_settings(app: tauri::AppHandle) -> SettingsView {
    let settings = read_settings(&app);
    SettingsView {
        control_url: settings.control_url,
        domain: settings.domain,
        has_token: matches!(fleet::read_token(), Ok(Some(_))),
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
    let settings = Settings {
        control_url: control_url.trim().to_string(),
        domain: domain.trim().to_string(),
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            load_settings,
            save_settings,
            list_boxes
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
