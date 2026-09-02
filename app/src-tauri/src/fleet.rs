//! Everything that leaves this machine, and the one secret that does not.
//!
//! ## Why this is Rust and not JavaScript
//!
//! It would be shorter to `fetch()` the control plane from React. It would
//! also make this a browser page with an installer, and give up the two things
//! a desktop shell exists for:
//!
//! - **No CORS.** A request from here is not a browser request, so the control
//!   plane and the door need no cross-origin headers. A page served at
//!   `localhost` calling `https://…up.railway.app` would need them on every
//!   endpoint, including the WebSocket upgrade the conversation uses.
//! - **The token never reaches the webview.** It is read from the OS keychain
//!   into a request header and dropped. JavaScript never holds it, so anything
//!   that gets script execution in the webview — a rendered agent reply, a
//!   dependency — cannot read it.
//!
//! The rule that keeps this true: **no URL of ours is ever fetched from the
//! frontend.** If that changes, the reason for the whole shell is gone.

use serde::{Deserialize, Serialize};
use std::time::Duration;

/// One keychain entry, named so a human browsing Keychain Access can tell what
/// it is and delete it deliberately.
const KEYCHAIN_SERVICE: &str = "dev.flotta.app";
const KEYCHAIN_ACCOUNT: &str = "control-plane-token";

/// Short. A control plane that is down should cost a few seconds, not a
/// spinner that never resolves.
const TIMEOUT: Duration = Duration::from_secs(15);

/// The non-secret half of the configuration.
///
/// In a plain file rather than the keychain because it is not a secret, and
/// because someone debugging "why is it talking to the wrong fleet" should be
/// able to read the answer without unlocking anything.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Settings {
    #[serde(default)]
    pub control_url: String,
    /// The domain boxes are addressed under: `<box>.<domain>`. Used by the
    /// conversation (M8.2); carried here so both halves read one setting.
    #[serde(default)]
    pub domain: String,
}

/// What went wrong, in the terms the person looking at it can act on.
///
/// Modelled rather than stringified because the three failures have three
/// different fixes and the empty state must not blur them: a fleet with no
/// agents, a control plane that cannot be reached, and a token that was
/// refused all render as "nothing here" if the UI is given only an error
/// string and a list.
#[derive(Debug, Serialize)]
#[serde(tag = "kind", content = "detail", rename_all = "snake_case")]
pub enum FleetError {
    /// No control-plane URL or no token — the app has never been set up.
    NotConfigured(String),
    /// DNS, TLS, timeout: the control plane did not answer.
    Unreachable(String),
    /// It answered, and said no. Carries the control plane's own words, which
    /// name the missing scope — better than anything this could infer.
    Rejected(String),
    /// It answered with something unexpected. Never silently an empty fleet.
    Unexpected(String),
}

/// A box, as the fleet API reports it. Deliberately a subset: this mirrors
/// what `GET /api/boxes` returns and nothing is computed here.
#[derive(Debug, Serialize, Deserialize)]
pub struct BoxRow {
    pub id: String,
    pub name: String,
    pub status: String,
    #[serde(default)]
    pub endpoint: Option<String>,
    #[serde(default)]
    pub created_at: Option<String>,
}

#[derive(Deserialize)]
struct BoxList {
    boxes: Vec<BoxRow>,
}

fn entry() -> Result<keyring::Entry, FleetError> {
    keyring::Entry::new(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
        .map_err(|e| FleetError::Unexpected(format!("keychain unavailable: {e}")))
}

pub fn read_token() -> Result<Option<String>, FleetError> {
    match entry()?.get_password() {
        Ok(token) => Ok(Some(token)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(FleetError::Unexpected(format!("keychain read failed: {e}"))),
    }
}

pub fn write_token(token: &str) -> Result<(), FleetError> {
    let entry = entry()?;
    if token.is_empty() {
        // Clearing is a real operation — signing out, or pasting the wrong
        // token and wanting it gone rather than overwritten with a blank.
        return match entry.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(e) => Err(FleetError::Unexpected(format!(
                "keychain clear failed: {e}"
            ))),
        };
    }
    entry
        .set_password(token)
        .map_err(|e| FleetError::Unexpected(format!("keychain write failed: {e}")))
}

/// GET a path on the control plane, with the token attached.
async fn get(settings: &Settings, path: &str) -> Result<String, FleetError> {
    let base = settings.control_url.trim().trim_end_matches('/');
    if base.is_empty() {
        return Err(FleetError::NotConfigured(
            "No control plane configured. Set its URL in Settings — it is the \
             https://… address the fleet API runs on."
                .into(),
        ));
    }
    let Some(token) = read_token()? else {
        return Err(FleetError::NotConfigured(
            "No access token. Mint one with `flotta token mint <you> --scope fleet:read \
             --scope box:chat` and paste it in Settings."
                .into(),
        ));
    };

    let client = reqwest::Client::builder()
        .timeout(TIMEOUT)
        .build()
        .map_err(|e| FleetError::Unexpected(e.to_string()))?;

    let response = client
        .get(format!("{base}{path}"))
        .bearer_auth(token)
        .send()
        .await
        .map_err(|e| {
            // Everything from here is "the control plane did not answer",
            // which is a different problem from it answering badly.
            FleetError::Unreachable(format!("could not reach {base}: {e}"))
        })?;

    let status = response.status();
    let body = response.text().await.unwrap_or_default();

    if status.as_u16() == 401 || status.as_u16() == 403 {
        // The control plane's 403 names the scope the token lacks. Passing it
        // through beats anything this layer could guess.
        return Err(FleetError::Rejected(detail_of(&body).unwrap_or_else(
            || format!("the control plane refused this token ({status})"),
        )));
    }
    if !status.is_success() {
        return Err(FleetError::Unexpected(format!(
            "{path} answered {status}: {}",
            detail_of(&body).unwrap_or_else(|| body.chars().take(200).collect())
        )));
    }
    Ok(body)
}

/// FastAPI puts its message in `{"detail": …}`. Best-effort by design: a body
/// that is not JSON is not an error worth reporting *about* the error.
fn detail_of(body: &str) -> Option<String> {
    serde_json::from_str::<serde_json::Value>(body)
        .ok()?
        .get("detail")?
        .as_str()
        .map(str::to_owned)
}

pub async fn list_boxes(settings: &Settings) -> Result<Vec<BoxRow>, FleetError> {
    let body = get(settings, "/api/boxes").await?;
    serde_json::from_str::<BoxList>(&body)
        .map(|list| list.boxes)
        .map_err(|e| {
            FleetError::Unexpected(format!(
                "the fleet API returned something unexpected ({e}); its shape may have changed"
            ))
        })
}
