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

/// For the calls that reach a *machine* rather than the fleet database.
///
/// Writing an agent's instructions starts its box if it is asleep, then runs a
/// command on it. Fifteen seconds is right for a database read and wrong for
/// that: the first version of this shipped on the short timeout and every save
/// came back "could not reach the control plane" while the request was still
/// in flight — an error blaming the network for a deadline we set.
///
/// Still bounded, and deliberately shorter than the control plane's own
/// `exec` timeout, so a hung machine surfaces here as a save that failed
/// rather than a button that never returns.
const MACHINE_TIMEOUT: Duration = Duration::from_secs(120);

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
    /// The keychain would not release the token. **Local**, and nothing to do
    /// with the control plane — which is why it is not `Unexpected`: the first
    /// version reported a denied keychain read under the heading "Unexpected
    /// answer from the control plane", about a control plane that had not been
    /// contacted at all.
    Keychain(String),
}

impl FleetError {
    /// The sentence, without the variant wrapped around it.
    ///
    /// `format!("{err:?}")` put `Unexpected("keychain read failed: …")` on
    /// screen, wrapper and quotes included. A person reading an error should
    /// not have to parse Rust's Debug output to find the message inside it.
    pub fn detail(&self) -> &str {
        match self {
            Self::NotConfigured(d)
            | Self::Unreachable(d)
            | Self::Rejected(d)
            | Self::Unexpected(d)
            | Self::Keychain(d) => d,
        }
    }
}

/// A box, as the fleet API reports it. Deliberately a subset: this mirrors
/// what `GET /api/boxes` returns and nothing is computed here.
#[derive(Debug, Serialize, Deserialize)]
pub struct BoxRow {
    pub id: String,
    /// The ADDRESS — a DNS label, immutable. Not what a person calls it.
    pub name: String,
    pub status: String,
    #[serde(default)]
    pub endpoint: Option<String>,
    #[serde(default)]
    pub created_at: Option<String>,
    /// What a person calls it (FLOTTA-40). `None` is "not set", which the
    /// window renders by falling back to the address — never as `undefined`.
    #[serde(default)]
    pub display_name: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    /// The standing instructions last written to the agent's volume. The copy
    /// it reads is `SOUL.md` there; this is the record of what we put in it,
    /// and the two diverge once an agent edits its own.
    #[serde(default)]
    pub instructions: Option<String>,
    /// When those instructions were last written, in **seconds since the Unix
    /// epoch** — the same units as a Hermes session's `started_at`, so the
    /// decision to resume or start fresh is a subtraction rather than a date
    /// parse.
    ///
    /// Only the single-box read fills this in; the fleet list leaves it
    /// `None`, because it is a query per box on the path that redraws the
    /// sidebar on a timer.
    #[serde(default)]
    pub instructions_changed_at: Option<f64>,
    /// The model the agent runs (FLOTTA-39): its own, or the fleet's.
    #[serde(default)]
    pub model: Option<String>,
    /// `agent` or `fleet`; `None` when the fleet has no model configured.
    #[serde(default)]
    pub model_source: Option<String>,
}

#[derive(Deserialize)]
struct BoxList {
    boxes: Vec<BoxRow>,
}

/// One line of a box's timeline, as `GET /api/boxes/{id}/events` reports it.
///
/// `type` is a Rust keyword, so the field is `kind` here and serde carries the
/// wire name in both directions — the webview reads `type`, the same word the
/// API and the store use. Renaming it for JavaScript's benefit would put three
/// spellings on one field.
///
/// `payload` is deliberately untyped. Its shape depends on the event: a
/// `torn_down` carries a `reason`, an `identity_minted` an `expires_at`, and
/// `reconcile` writes whatever it learned. Modelling that here would mean
/// updating Rust every time the control plane records something new, and the
/// app only ever reads a couple of keys out of it.
///
/// **`entity_kind` is not optional, and dropping it was a bug.** The endpoint
/// is box-scoped but the timeline is not: `get_box_timeline` unions the box's
/// own events with those of its tasks *and* its workspaces, because "what has
/// this agent been doing" spans all three tiers. Without this field, "the
/// reason this box was torn down" is really "the last `torn_down` from any
/// tier" — a workspace teardown shown as the agent's own fate. It reads
/// correctly today only because `teardown_box` happens to write the workspace
/// events first, which is ordering luck, not a guarantee.
///
/// Required rather than defaulted, deliberately. Guessing `box` would put the
/// bug back for any event that stopped saying what it belonged to; failing to
/// parse is loud, and the pane shows that error rather than a confident wrong
/// answer.
#[derive(Debug, Serialize, Deserialize)]
pub struct BoxEvent {
    pub id: i64,
    pub ts: String,
    /// `box`, `task` or `workspace`.
    pub entity_kind: String,
    #[serde(rename = "type")]
    pub kind: String,
    #[serde(default)]
    pub payload: Option<serde_json::Value>,
}

/// One thing about the fleet a person may change, as `GET /api/settings`
/// reports it.
///
/// The catalogue travels with the value — `label`, `help`, `kind` and
/// `default` all come from the control plane — so the window renders a form it
/// does not hardcode. A setting added server-side appears here without the app
/// being rebuilt, which is the point of the control plane owning the list.
///
/// `source` is `store`, `env` or `default`, and it earns its place: "why is my
/// fleet not using the number I set" is otherwise unanswerable from a window,
/// and the answer is usually a deployment variable still in play.
///
/// **Nothing in here is ever a credential.** The control plane's catalogue is
/// an allowlist of configuration, so the settings API has no secret to return
/// and this struct has nowhere to put one.
/// Defaults to settable, so a control plane that predates `editable` renders
/// exactly as it does today rather than showing every field greyed out.
fn yes() -> bool {
    true
}

#[derive(Debug, Serialize, Deserialize)]
pub struct FleetSetting {
    pub key: String,
    pub label: String,
    pub help: String,
    pub kind: String,
    pub default: String,
    pub value: String,
    pub source: String,
    /// Whether the app may change it. A shown-but-not-settable value is one
    /// a person needs to see and that cannot safely be written over the API —
    /// the provider endpoint, which decides where the fleet's key is sent.
    #[serde(default = "yes")]
    pub editable: bool,
}

/// What the substrate says about a box's machine, right now.
///
/// Every other read the app makes is the store's **belief** — written by
/// whatever last reached the substrate. Usually the two agree. When they do
/// not, that is the interesting part: Fly stops a machine during a host drain
/// and the row still says `running`; an agent upgraded from another laptop
/// runs an image this fleet's row has never mentioned.
///
/// So the panel shows both, unmerged. Every field but `state` is optional
/// because this is `flyctl`'s JSON at two removes, and a key that moves should
/// blank one line rather than fail the whole read.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Machine {
    pub state: String,
    #[serde(default)]
    pub machine_id: Option<String>,
    #[serde(default)]
    pub app: Option<String>,
    #[serde(default)]
    pub image: Option<String>,
    /// Which Hermes this image carries, from a label baked in at build time.
    /// `None` for an image built before that label existed, which is not the
    /// same as "no Hermes" and must not be rendered as one.
    #[serde(default)]
    pub hermes_ref: Option<String>,
    #[serde(default)]
    pub region: Option<String>,
    #[serde(default)]
    pub cpu_kind: Option<String>,
    #[serde(default)]
    pub cpus: Option<u32>,
    #[serde(default)]
    pub memory_mb: Option<u32>,
    #[serde(default)]
    pub volume_id: Option<String>,
    #[serde(default)]
    pub volume_gb: Option<u32>,
    #[serde(default)]
    pub volume_path: Option<String>,
    #[serde(default)]
    pub private_ip: Option<String>,
    #[serde(default)]
    pub created_at: Option<String>,
    #[serde(default)]
    pub updated_at: Option<String>,
    #[serde(default)]
    pub host_status: Option<String>,
    /// Where this agent clones projects (`FLOTTA_WORKDIR` on the machine).
    /// `None` when the substrate did not report it — agents created before
    /// the setting was wired still use the entrypoint default `/workspace`.
    #[serde(default)]
    pub workdir: Option<String>,
}

/// The row and the machine together, plus why the machine is missing when it
/// is. All three are needed: a panel with no row has nothing to show while the
/// substrate is unreachable, and a missing machine with no reason is the
/// "No agents yet" failure again, one screen along.
#[derive(Debug, Serialize, Deserialize)]
pub struct MachineView {
    #[serde(rename = "box")]
    pub row: BoxRow,
    #[serde(default)]
    pub machine: Option<Machine>,
    #[serde(default)]
    pub unavailable: Option<String>,
    /// What an upgrade with no argument would move this agent onto.
    #[serde(default)]
    pub fleet_image: Option<String>,
    /// Whether it is already on that image. **`None` is not `false`** — one
    /// side being unknown must not put an Upgrade button in front of anyone.
    /// Computed on the control plane with `_same_image`, because Fly reports a
    /// digest the configured image does not carry and comparing them as
    /// strings has been wrong three times here.
    #[serde(default)]
    pub image_current: Option<bool>,
}

/// Which Hermes the fleet builds, and whether a newer one exists.
///
/// Three facts that are easy to collapse into one and must not be: `pinned` is
/// what the *next* image would be built from, `latest` is what upstream has,
/// and what an individual agent runs is neither — that is `Machine::hermes_ref`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HermesVersions {
    /// What the *next* image would be built at. An intention: after an update
    /// started from the window it stays put while the fleet moves on.
    pub pinned: String,
    /// What the fleet's newest image was actually built at — the fact
    /// `behind` is measured against.
    #[serde(default)]
    pub fleet_ref: Option<String>,
    /// `build` or `pin`.
    #[serde(default)]
    pub fleet_ref_source: Option<String>,
    /// `None` when GitHub could not be reached. Renders as *unknown*, never as
    /// up to date.
    #[serde(default)]
    pub latest: Option<String>,
    #[serde(default)]
    pub behind: bool,
    #[serde(default)]
    pub unavailable: Option<String>,
    #[serde(default)]
    pub fleet_image: Option<String>,
    /// `env`, `release` or `none` — where `fleet_image` came from.
    #[serde(default)]
    pub fleet_image_source: Option<String>,
    /// What the build app last released. Reported even when the environment
    /// won, because the two disagreeing is the failure worth seeing.
    #[serde(default)]
    pub newest_release: Option<String>,
}

#[derive(Deserialize)]
struct SettingList {
    settings: Vec<FleetSetting>,
}

#[derive(Deserialize)]
struct EventList {
    events: Vec<BoxEvent>,
}

fn entry() -> Result<keyring::Entry, FleetError> {
    keyring::Entry::new(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
        .map_err(|e| FleetError::Keychain(format!("the keychain is unavailable: {e}")))
}

/// A development-only escape hatch, and the reason it is not a weakening.
///
/// An unsigned binary's keychain ACL is tied to that exact binary, so **every**
/// `cargo` rebuild is denied access to the item the previous one created. In
/// release builds, signed with a stable identity, this never happens. In
/// development it happens constantly — and re-pasting a token after every
/// rebuild is the kind of friction that ends with someone hard-coding it.
///
/// So in debug builds only, `$FLOTTA_TOKEN` is honoured — the same variable
/// `flotta chat` already reads, so there is nothing new to manage. It is
/// compiled out of release builds entirely, not merely skipped: `cfg` removes
/// the code, so no shipped binary can be made to read a token from the
/// environment by setting one.
#[cfg(debug_assertions)]
fn token_from_env() -> Option<String> {
    std::env::var("FLOTTA_TOKEN")
        .ok()
        .map(|t| t.trim().to_string())
        .filter(|t| !t.is_empty())
}

#[cfg(not(debug_assertions))]
fn token_from_env() -> Option<String> {
    None
}

pub fn read_token() -> Result<Option<String>, FleetError> {
    // Before the keychain, so a developer whose item has been orphaned by a
    // rebuild is not blocked by it.
    if let Some(token) = token_from_env() {
        return Ok(Some(token));
    }

    match entry()?.get_password() {
        Ok(token) => Ok(Some(token)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(FleetError::Keychain(format!("{e}"))),
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
    // Write, and on refusal delete-then-write.
    //
    // `set_password` *updates* an existing item, and updating needs access to
    // it — which is exactly what a rebuilt binary is denied on macOS, since an
    // unsigned build's keychain ACL is tied to that build. So the obvious
    // recovery ("just save the token again") failed with the same error as the
    // read, and the app had no way to repair itself from the inside.
    //
    // Creating a fresh item needs no access to the old one. Deleting usually
    // does not either, because the item's ACL governs reading its *secret*.
    if let Err(first) = entry.set_password(token) {
        let recovered = entry
            .delete_credential()
            .ok()
            .and_then(|()| keyring::Entry::new(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT).ok())
            .map(|fresh| fresh.set_password(token));

        match recovered {
            Some(Ok(())) => return Ok(()),
            _ => {
                return Err(FleetError::Keychain(format!(
                    "saving it failed: {first}. The existing keychain item will not \
                     accept this build. Remove it and try again:\n\n    security \
                     delete-generic-password -s {KEYCHAIN_SERVICE} -a {KEYCHAIN_ACCOUNT}"
                )));
            }
        }
    }
    Ok(())
}

/// GET a path on the control plane, with the token attached.
/// The three things every call needs, or a reason it cannot be made.
fn prepare(settings: &Settings) -> Result<(String, String, reqwest::Client), FleetError> {
    let base = settings
        .control_url
        .trim()
        .trim_end_matches('/')
        .to_string();
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
    Ok((base, token, client))
}

async fn get(settings: &Settings, path: &str) -> Result<String, FleetError> {
    let (base, token, client) = prepare(settings)?;
    let response = client
        .get(endpoint(&base, path))
        .bearer_auth(token)
        .send()
        .await
        .map_err(|e| {
            // Everything from here is "the control plane did not answer",
            // which is a different problem from it answering badly.
            FleetError::Unreachable(format!("could not reach {base}: {e}"))
        })?;

    let status = response.status().as_u16();
    let body = response.text().await.unwrap_or_default();

    match classify(status, path, &body) {
        Some(error) => Err(error),
        None => Ok(body),
    }
}

/// One path segment, safe to interpolate.
///
/// Ids are `b-<hex>` and could be dropped in raw, but `get_box` and
/// `box_events` both accept a **name** as well, and a name is not validated
/// anywhere — `store.create_box` inserts the string it is handed, and
/// FLOTTA-28's tombstones are `<name>@<id>`. A slash in one would silently
/// address a different endpoint rather than fail.
///
/// Hand-rolled rather than pulling in `percent-encoding`: the unreserved set
/// is the whole job, and a dependency for twelve lines is not worth the
/// supply chain.
fn encode_segment(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for byte in value.as_bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => {
                out.push(*byte as char)
            }
            other => out.push_str(&format!("%{other:02X}")),
        }
    }
    out
}

/// `{base}{path}`, with exactly one slash between them.
///
/// Its own function so it can be tested: a trailing slash on a pasted URL is
/// the most likely thing to be wrong about a setting, and `//api/boxes` is a
/// 404 that reads like a missing endpoint rather than a typo.
fn endpoint(base: &str, path: &str) -> String {
    format!("{}{path}", base.trim().trim_end_matches('/'))
}

/// Which failure a response is, or `None` when it is not one.
///
/// Separated from the request so the mapping can be tested without a server.
/// It is the part that decides which of three messages a person sees, and the
/// distinction between them is the whole reason this module exists.
fn classify(status: u16, path: &str, body: &str) -> Option<FleetError> {
    if status == 401 || status == 403 {
        // The control plane's 403 names the scope the token lacks. Passing it
        // through beats anything this layer could guess.
        return Some(FleetError::Rejected(detail_of(body).unwrap_or_else(|| {
            format!("the control plane refused this token ({status})")
        })));
    }
    // **The app and the control plane version independently**, and this is the
    // first place that shows. The app is installed on a laptop; the control
    // plane is deployed somewhere else and updated on its own schedule, so a
    // window running ahead of its fleet is normal rather than exceptional.
    //
    // A bare "answered 404: Not Found" is true and useless — it reads as the
    // app being broken. Named per path rather than globally, because
    // `/api/boxes/{id}` answering 404 means something entirely different and
    // correct: there is no such agent.
    if status == 404 && path == "/api/settings" {
        return Some(FleetError::Unexpected(
            "this control plane is older than the app and has no fleet settings yet. \
             Deploy the current version of the control plane, or keep setting these \
             where it runs."
                .into(),
        ));
    }
    // The same skew, one endpoint later — but here 404 is genuinely ambiguous:
    // this route answers it for an agent that does not exist, and FastAPI
    // answers it for a route that does not exist. They are told apart by the
    // body: the endpoint's 404 names the box (`no box 'eng-q'`), while a
    // missing route is FastAPI's bare `Not Found`.
    if status == 404 && path.ends_with("/machine") {
        let detail = detail_of(body);
        if detail.is_none() || detail.as_deref() == Some("Not Found") {
            return Some(FleetError::Unexpected(
                "this control plane is older than the app and cannot report machine \
                 details yet. Deploy the current version of the control plane."
                    .into(),
            ));
        }
        return Some(FleetError::Unexpected(detail.unwrap()));
    }
    if !(200..300).contains(&status) {
        return Some(FleetError::Unexpected(format!(
            "{path} answered {status}: {}",
            detail_of(body).unwrap_or_else(|| body.chars().take(200).collect())
        )));
    }
    None
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

/// POST/DELETE a path. Same auth and error handling as `get`.
async fn send(
    settings: &Settings,
    method: reqwest::Method,
    path: &str,
    body: Option<serde_json::Value>,
) -> Result<String, FleetError> {
    send_within(settings, method, path, body, None).await
}

/// `send`, with a deadline of its own for the calls that reach a machine.
///
/// Per-request rather than a second client: the override is the exception, and
/// building a client per call would lose connection reuse for every ordinary
/// read to save one line here.
async fn send_within(
    settings: &Settings,
    method: reqwest::Method,
    path: &str,
    body: Option<serde_json::Value>,
    timeout: Option<Duration>,
) -> Result<String, FleetError> {
    let (base, token, client) = prepare(settings)?;
    let mut request = client
        .request(method, endpoint(&base, path))
        .bearer_auth(token);
    if let Some(timeout) = timeout {
        request = request.timeout(timeout);
    }
    if let Some(body) = body {
        request = request.json(&body);
    }
    let response = request
        .send()
        .await
        .map_err(|e| FleetError::Unreachable(format!("could not reach {base}: {e}")))?;

    let status = response.status().as_u16();
    let text = response.text().await.unwrap_or_default();
    match classify(status, path, &text) {
        Some(error) => Err(error),
        None => Ok(text),
    }
}

/// Everything the Create form can say about a new agent.
///
/// One struct rather than one argument per field: the form grew a field per
/// milestone (size, region, identity, instructions, and now a model), and a
/// command with eight positional arguments is one where two `Option<String>`s
/// can be swapped without the compiler noticing.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct NewAgent {
    pub name: String,
    #[serde(default)]
    pub volume_gb: Option<u32>,
    #[serde(default)]
    pub region: Option<String>,
    #[serde(default)]
    pub display_name: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub instructions: Option<String>,
    /// A model id, when not the fleet's (FLOTTA-39).
    #[serde(default)]
    pub model: Option<String>,
}

/// The body `POST /api/boxes` is sent.
///
/// Omitted, not nulled, when the agent takes the fleet default — which is the
/// common case. A body carrying `"volume_gb": null` would work, but "this
/// field is absent" and "this field is deliberately nothing" are worth keeping
/// distinct on a wire somebody will read in a log one day.
fn create_body(agent: &NewAgent) -> Result<serde_json::Value, FleetError> {
    let name = agent.name.trim();
    if name.is_empty() {
        return Err(FleetError::Unexpected("an agent needs a name".into()));
    }
    let mut body = serde_json::Map::new();
    body.insert("name".into(), serde_json::Value::from(name));
    // Only a value that was actually typed goes on the wire.
    for (key, value) in [
        ("display_name", &agent.display_name),
        ("description", &agent.description),
        ("instructions", &agent.instructions),
        ("region", &agent.region),
        ("model", &agent.model),
    ] {
        if let Some(v) = value.as_deref().map(str::trim).filter(|v| !v.is_empty()) {
            body.insert(key.into(), serde_json::Value::from(v));
        }
    }
    if let Some(gb) = agent.volume_gb {
        body.insert("volume_gb".into(), serde_json::Value::from(gb));
    }
    Ok(serde_json::Value::Object(body))
}

/// Create an agent.
///
/// One request, and the box comes back with its identity already on it —
/// FLOTTA-21 injects it at creation, which is why this is a button and not a
/// button followed by a terminal.
pub async fn create_box(settings: &Settings, agent: &NewAgent) -> Result<BoxRow, FleetError> {
    let body = create_body(agent)?;
    let body = send(settings, reqwest::Method::POST, "/api/boxes", Some(body)).await?;

    // Since FLOTTA-27 the usual answer is `202` with a box that is still
    // `provisioning` — a machine appears a minute or two later. `classify`
    // treats every 2xx alike, which is right: `201`, `201`-with-a-warning (a
    // machine that was made but is not running) and `202` all mean something
    // real was created and is billing, and none of them is a failure.
    box_from(&body, "created, but could not read it back")
}

/// Change the model an agent runs; `None` puts it back on the fleet's.
///
/// Waits as long as renewing an identity does, for the same reason: a
/// running agent restarts before the control plane answers.
pub async fn set_model(
    settings: &Settings,
    id: &str,
    model: Option<String>,
) -> Result<BoxRow, FleetError> {
    let model = model
        .map(|m| m.trim().to_string())
        .filter(|m| !m.is_empty());
    let body = send_within(
        settings,
        reqwest::Method::PUT,
        &format!("/api/boxes/{}/model", encode_segment(id)),
        // `null`, not absent: the route refuses a body with no `model` key,
        // because an empty body is more likely a bug than a reset.
        Some(serde_json::json!({ "model": model })),
        Some(ROTATE_TIMEOUT),
    )
    .await?;
    box_from(&body, "changed, but could not read the agent back")
}

/// A `BoxRow` out of a body that may or may not wrap it.
///
/// `POST /api/boxes` answers `{"box": …}` — plus `warning`, or `box_id` and
/// `status` on the async path — and `GET /api/boxes/{id}` answers `{"box": …,
/// "tasks": …}`. Falling back to the whole body keeps this working if either
/// ever answers with the row on its own.
fn box_from(body: &str, what: &str) -> Result<BoxRow, FleetError> {
    let value: serde_json::Value = serde_json::from_str(body)
        .map_err(|e| FleetError::Unexpected(format!("unreadable answer: {e}")))?;
    let row = value.get("box").unwrap_or(&value);
    serde_json::from_value(row.clone()).map_err(|e| FleetError::Unexpected(format!("{what}: {e}")))
}

/// Destroy an agent, and everything it remembers.
pub async fn destroy_box(settings: &Settings, id: &str) -> Result<(), FleetError> {
    send(
        settings,
        reqwest::Method::DELETE,
        &format!("/api/boxes/{id}"),
        None,
    )
    .await
    .map(|_| ())
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

/// One agent by id or name, **including a torn-down one**.
///
/// That exception is the whole reason this exists beside `list_boxes`. The
/// list endpoint filters terminal boxes, on purpose — a destroyed agent should
/// not clutter a fleet — but it means a provision that fails does not leave a
/// row explaining itself: the agent simply stops being in the answer. This is
/// how the app can still say what became of one it was watching.
pub async fn get_box(settings: &Settings, id: &str) -> Result<BoxRow, FleetError> {
    let body = get(settings, &format!("/api/boxes/{}", encode_segment(id))).await?;
    box_from(&body, "could not read that agent")
}

/// How the fleet is configured, and where each value came from.
pub async fn fleet_settings(settings: &Settings) -> Result<Vec<FleetSetting>, FleetError> {
    let body = get(settings, "/api/settings").await?;
    settings_from(&body)
}

/// Change fleet settings. An empty value clears an override.
///
/// The whole map goes in one request because the control plane validates it
/// as one: a bad value refuses the lot rather than applying half, which is
/// what stops the fleet ending up in a state nobody asked for while the form
/// shows something else.
pub async fn set_fleet_settings(
    settings: &Settings,
    values: serde_json::Value,
) -> Result<Vec<FleetSetting>, FleetError> {
    let body = send(
        settings,
        reqwest::Method::PUT,
        "/api/settings",
        Some(serde_json::json!({ "values": values })),
    )
    .await?;
    settings_from(&body)
}

fn settings_from(body: &str) -> Result<Vec<FleetSetting>, FleetError> {
    serde_json::from_str::<SettingList>(body)
        .map(|list| list.settings)
        .map_err(|e| FleetError::Unexpected(format!("unreadable settings: {e}")))
}

/// A box's timeline: what happened to it, oldest first.
///
/// The app reads this for the one question the fleet list cannot answer —
/// *why*. A failed provision writes `torn_down` with a reason and leaves the
/// list; the reason is here and nowhere else the app can reach.
pub async fn box_events(settings: &Settings, id: &str) -> Result<Vec<BoxEvent>, FleetError> {
    let body = get(
        settings,
        &format!("/api/boxes/{}/events", encode_segment(id)),
    )
    .await?;
    serde_json::from_str::<EventList>(&body)
        .map(|list| list.events)
        .map_err(|e| FleetError::Unexpected(format!("unreadable timeline: {e}")))
}

/// What the substrate says about one agent's machine.
///
/// **Never polled.** It is a `flyctl` subprocess on the control plane, so it
/// is asked when a person opens the panel and not on a timer. The list view
/// stays on the store, which is cheap and, for status, written by every verb
/// that touches a box.
pub async fn machine(settings: &Settings, id: &str) -> Result<MachineView, FleetError> {
    let body = get(
        settings,
        &format!("/api/boxes/{}/machine", encode_segment(id)),
    )
    .await?;
    serde_json::from_str::<MachineView>(&body)
        .map_err(|e| FleetError::Unexpected(format!("unreadable machine info: {e}")))
}

/// Which Hermes the fleet builds, and whether upstream has a newer one.
pub async fn hermes_versions(settings: &Settings) -> Result<HermesVersions, FleetError> {
    let body = get(settings, "/api/hermes").await?;
    serde_json::from_str::<HermesVersions>(&body)
        .map_err(|e| FleetError::Unexpected(format!("unreadable Hermes versions: {e}")))
}

/// Move an agent onto a new image, keeping its disk.
///
/// Answers `202` and re-images on a thread, so this returns as soon as the
/// control plane has accepted it — the outcome arrives in the agent's timeline
/// as `reimaged` or `upgrade_failed`. A `409` here means the agent cannot be
/// upgraded at all and is worth showing; anything else is the substrate.
pub async fn upgrade_agent(settings: &Settings, id: &str) -> Result<(), FleetError> {
    send(
        settings,
        reqwest::Method::POST,
        &format!("/api/boxes/{}/upgrade", encode_segment(id)),
        Some(serde_json::json!({})),
    )
    .await
    .map(|_| ())
}

/// One attempt to build the box image. Mirrors `Build` in `store.py`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Build {
    pub id: String,
    pub hermes_ref: String,
    /// `building`, `rolling`, `done` or `failed`. `rolling` is still running:
    /// the image exists and agents are being moved onto it one at a time.
    pub status: String,
    #[serde(default)]
    pub image: Option<String>,
    #[serde(default)]
    pub error: Option<String>,
    pub started_at: String,
    #[serde(default)]
    pub finished_at: Option<String>,
}

#[derive(Deserialize)]
struct BuildList {
    builds: Vec<Build>,
}

/// Build the box image at a Hermes ref and roll every agent onto it.
///
/// Answers `202` — a cold build is minutes — so this returns as soon as the
/// control plane has accepted the job. Progress is `builds()` plus the usual
/// per-agent timeline events.
pub async fn update_hermes(settings: &Settings, hermes_ref: &str) -> Result<(), FleetError> {
    send(
        settings,
        reqwest::Method::POST,
        "/api/hermes/update",
        Some(serde_json::json!({ "hermes_ref": hermes_ref })),
    )
    .await
    .map(|_| ())
}

/// What the fleet has built, newest first.
pub async fn builds(settings: &Settings) -> Result<Vec<Build>, FleetError> {
    let body = get(settings, "/api/hermes/builds").await?;
    serde_json::from_str::<BuildList>(&body)
        .map(|list| list.builds)
        .map_err(|e| FleetError::Unexpected(format!("unreadable build list: {e}")))
}

/// Rename or re-describe an agent. The address never changes, and neither do
/// the instructions — those live on the agent's own volume; see the control
/// plane's `PUT /api/boxes/{id}/meta` for why.
pub async fn rename_agent(
    settings: &Settings,
    id: &str,
    display_name: Option<String>,
    description: Option<String>,
) -> Result<BoxRow, FleetError> {
    let body = send(
        settings,
        reqwest::Method::PUT,
        &format!("/api/boxes/{}/meta", encode_segment(id)),
        Some(serde_json::json!({
            "display_name": display_name,
            "description": description,
        })),
    )
    .await?;
    box_from(&body, "could not rename that agent")
}

/// The repositories an agent may use.
///
/// Every one of the three endpoints answers with the **whole list** after the
/// change, so the window never has to reason about what a grant did — it
/// replaces what it is showing with what the fleet now says. That also carries
/// the normalisation for free: a pasted URL comes back as `owner/name`, so what
/// is displayed is what was actually granted.
#[derive(Debug, Clone, Deserialize)]
struct RepoList {
    #[serde(default)]
    repos: Vec<String>,
}

/// Which repositories this agent may clone, commit to and push.
pub async fn list_repos(settings: &Settings, id: &str) -> Result<Vec<String>, FleetError> {
    let body = get(
        settings,
        &format!("/api/boxes/{}/repos", encode_segment(id)),
    )
    .await?;
    repos_from(&body)
}

/// Grant one. Idempotent, and the control plane decides what the text meant:
/// a slug, an https URL and an ssh URL are one repository, and normalising
/// here as well would be a second opinion that can drift from the first.
pub async fn grant_repo(
    settings: &Settings,
    id: &str,
    repo: &str,
) -> Result<Vec<String>, FleetError> {
    let body = send(
        settings,
        reqwest::Method::POST,
        &format!("/api/boxes/{}/repos", encode_segment(id)),
        Some(serde_json::json!({ "repo": repo })),
    )
    .await?;
    repos_from(&body)
}

/// Withdraw one. Takes effect on the box's next fetch, with no restart —
/// there is nothing on the machine to invalidate, which is the point of the
/// box holding no credential.
pub async fn revoke_repo(
    settings: &Settings,
    id: &str,
    repo: &str,
) -> Result<Vec<String>, FleetError> {
    let (owner, name) = split_repo(repo)?;
    let body = send(
        settings,
        reqwest::Method::DELETE,
        &format!(
            "/api/boxes/{}/repos/{}/{}",
            encode_segment(id),
            encode_segment(&owner),
            encode_segment(&name),
        ),
        None,
    )
    .await?;
    repos_from(&body)
}

/// `owner/name` as two path segments.
///
/// The revoke route spells the repository as a path rather than a body, so it
/// has to be split — and split *here*, not by string surgery at the call site,
/// because getting it wrong builds a URL that deletes something else or
/// nothing at all. Anything that is not exactly two segments is refused rather
/// than guessed at.
fn split_repo(repo: &str) -> Result<(String, String), FleetError> {
    // Strict, and deliberately stricter than `normalise_repo` on the Python
    // side: that one *normalises* what a person typed, and dropping an empty
    // segment there is a kindness. This one splits a value the control plane
    // has already normalised, so an empty segment means something upstream is
    // wrong — and quietly turning `a//b` into `a/b` would send a DELETE for a
    // repository nobody named.
    let parts: Vec<&str> = repo.split('/').collect();
    match parts.as_slice() {
        [owner, name] if !owner.is_empty() && !name.is_empty() => {
            Ok((owner.to_string(), name.to_string()))
        }
        _ => Err(FleetError::Unexpected(format!(
            "{repo:?} does not name a repository as owner/name"
        ))),
    }
}

fn repos_from(body: &str) -> Result<Vec<String>, FleetError> {
    let value: RepoList = serde_json::from_str(body)
        .map_err(|e| FleetError::Unexpected(format!("unreadable repository list: {e}")))?;
    Ok(value.repos)
}

/// An agent this agent may message (M7).
///
/// The address and what the agent is for, together: a person granting a
/// colleague picks by the second, and the agent then has to type the first.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Peer {
    pub id: String,
    pub name: String,
    #[serde(default)]
    pub display_name: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
}

/// Who an agent may ask, and who it has been stopped from asking (M7).
///
/// **Everyone is a colleague by default** (FLOTTA-65); `blocked` is the list
/// of exceptions. Every endpoint answers with both after a change, so the
/// window shows what the fleet now says rather than patching what it showed.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Colleagues {
    #[serde(default)]
    pub peers: Vec<Peer>,
    #[serde(default)]
    pub blocked: Vec<Peer>,
}

/// Both lists for one agent.
pub async fn colleagues(settings: &Settings, id: &str) -> Result<Colleagues, FleetError> {
    let body = get(
        settings,
        &format!("/api/boxes/{}/peers", encode_segment(id)),
    )
    .await?;
    colleagues_from(&body)
}

/// Stop this agent asking `peer`. **One way**: `peer` may still ask it.
pub async fn block_peer(
    settings: &Settings,
    id: &str,
    peer: &str,
) -> Result<Colleagues, FleetError> {
    let body = send(
        settings,
        reqwest::Method::POST,
        &format!("/api/boxes/{}/peer-blocks", encode_segment(id)),
        Some(serde_json::json!({ "peer": peer })),
    )
    .await?;
    colleagues_from(&body)
}

/// Lift a block. Takes effect on the next message, with no restart.
pub async fn allow_peer(
    settings: &Settings,
    id: &str,
    peer: &str,
) -> Result<Colleagues, FleetError> {
    let body = send(
        settings,
        reqwest::Method::DELETE,
        &format!(
            "/api/boxes/{}/peer-blocks/{}",
            encode_segment(id),
            encode_segment(peer)
        ),
        None,
    )
    .await?;
    colleagues_from(&body)
}

fn colleagues_from(body: &str) -> Result<Colleagues, FleetError> {
    serde_json::from_str(body)
        .map_err(|e| FleetError::Unexpected(format!("unreadable list of colleagues: {e}")))
}

/// A fresh identity, as the control plane reports it. Never the token.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RotatedIdentity {
    pub box_id: String,
    pub name: String,
    /// Seconds since the Unix epoch.
    pub expires_at: f64,
    #[serde(default)]
    pub scopes: Vec<String>,
}

/// Longer than `MACHINE_TIMEOUT`: a running agent restarts, *and passes its
/// health checks*, before the control plane answers. Cutting that short would
/// report a rotation that succeeded as a failure.
const ROTATE_TIMEOUT: Duration = Duration::from_secs(180);

/// Give an agent a fresh identity token, written by the control plane to the
/// agent's own machine. A sleeping agent stays asleep.
pub async fn rotate_identity(settings: &Settings, id: &str) -> Result<RotatedIdentity, FleetError> {
    let body = send_within(
        settings,
        reqwest::Method::POST,
        &format!("/api/boxes/{}/identity", encode_segment(id)),
        None,
        Some(ROTATE_TIMEOUT),
    )
    .await?;
    serde_json::from_str(&body)
        .map_err(|e| FleetError::Unexpected(format!("unreadable identity answer: {e}")))
}

/// What was written to the agent's volume, and when.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WrittenInstructions {
    #[serde(default)]
    pub instructions: Option<String>,
    /// Seconds since the Unix epoch. `None` only if the control plane could
    /// not read back the event it had just written, which is not worth failing
    /// the save over — the window falls back to resetting unconditionally.
    #[serde(default)]
    pub changed_at: Option<f64>,
}

/// Rewrite an agent's standing instructions on its own volume.
///
/// Separate from `rename_agent` because it is a different kind of act: a
/// display name is a label the fleet keeps, while this wakes a machine and
/// writes to its disk. It can fail — a box that is torn down, a machine that
/// will not start — where renaming cannot.
pub async fn set_instructions(
    settings: &Settings,
    id: &str,
    instructions: Option<String>,
) -> Result<WrittenInstructions, FleetError> {
    let body = send_within(
        settings,
        reqwest::Method::PUT,
        &format!("/api/boxes/{}/instructions", encode_segment(id)),
        Some(serde_json::json!({ "instructions": instructions })),
        Some(MACHINE_TIMEOUT),
    )
    .await?;
    serde_json::from_str(&body)
        .map_err(|e| FleetError::Unexpected(format!("could not save those instructions: {e}")))
}

#[cfg(test)]
mod tests {
    use super::*;

    // These four decisions are the reason this module is Rust rather than a
    // `fetch()` in React, and none of them was covered until three reviewers
    // said so on the same PR.

    #[test]
    fn a_refusal_is_told_apart_from_a_failure() {
        // 401 and 403 mean "your token"; anything else non-2xx does not. The
        // app renders three different screens off this distinction, and
        // collapsing it is how an app tells you your fleet is empty when your
        // token was simply refused.
        assert!(matches!(
            classify(403, "/api/boxes", ""),
            Some(FleetError::Rejected(_))
        ));
        assert!(matches!(
            classify(401, "/api/boxes", ""),
            Some(FleetError::Rejected(_))
        ));
        assert!(matches!(
            classify(500, "/api/boxes", ""),
            Some(FleetError::Unexpected(_))
        ));
    }

    #[test]
    fn a_refusal_carries_the_control_planes_own_words() {
        // Its 403 names the missing scope. Anything this layer invented
        // instead would be less useful, and would go stale when the scopes
        // change.
        let body = r#"{"detail":"token for 'app' lacks scope(s): fleet:read"}"#;
        match classify(403, "/api/boxes", body) {
            Some(FleetError::Rejected(detail)) => {
                assert!(detail.contains("fleet:read"), "{detail}")
            }
            other => panic!("expected a rejection, got {other:?}"),
        }
    }

    #[test]
    fn success_is_not_an_error() {
        assert!(classify(200, "/api/boxes", "{}").is_none());
        assert!(classify(201, "/api/boxes", "{}").is_none());
    }

    #[test]
    fn a_body_that_is_not_json_does_not_break_the_error_path() {
        // An HTML error page from a proxy is the normal shape of "something
        // between here and the control plane answered". Failing to parse it
        // must not turn into a second, more confusing failure.
        match classify(502, "/api/boxes", "<html>Bad Gateway</html>") {
            Some(FleetError::Unexpected(detail)) => assert!(detail.contains("502"), "{detail}"),
            other => panic!("expected unexpected, got {other:?}"),
        }
    }

    #[test]
    fn a_trailing_slash_does_not_double_up() {
        // The likeliest thing to be wrong about a pasted URL, and `//api/boxes`
        // is a 404 that reads like a missing endpoint rather than a typo.
        assert_eq!(
            endpoint("https://fleet.example/", "/api/boxes"),
            "https://fleet.example/api/boxes"
        );
        assert_eq!(
            endpoint("  https://fleet.example  ", "/api/boxes"),
            "https://fleet.example/api/boxes"
        );
    }

    #[test]
    fn a_real_fleet_response_parses_to_the_boxes_it_contains() {
        // The literal body `GET /api/boxes` returned from the deployed control
        // plane, captured rather than hand-written: the app showed "No agents
        // yet" for a fleet that has one, and the parse was a suspect that
        // could only be cleared by feeding it the exact bytes.
        //
        // It carries fields the app does not model (`destroyed_at`,
        // `latest_task`, `task_count`, `cost_estimate`). Serde ignores unknown
        // fields by default — this asserts that stays true, because the day it
        // does not, every box silently disappears from the list.
        let body = include_str!("../tests/fixture.json");
        let list: BoxList = serde_json::from_str(body).expect("real response must parse");
        assert_eq!(list.boxes.len(), 1);
        assert_eq!(list.boxes[0].name, "eng-a");
        assert_eq!(list.boxes[0].status, "stopped");
        assert!(list.boxes[0].endpoint.is_some());
    }

    #[test]
    fn the_colleague_lists_are_read_as_the_control_plane_sends_them() {
        // The shape of `GET /api/boxes/{id}/peers` since FLOTTA-65. A
        // description can be absent, and an agent with none must still be
        // listed rather than failing the parse for every colleague.
        let body = r#"{"box_id":"b-79c6ca696e62","name":"eng-g",
            "peers":[{"id":"b-e0d678cb4684","name":"eng-r","display_name":"Reviewer - backend PRs",
                      "description":"Reviews backend pull requests"}],
            "blocked":[{"id":"b-1","name":"eng-d","display_name":null,"description":null}]}"#;
        let lists = colleagues_from(body).expect("real response must parse");
        assert_eq!(lists.peers.len(), 1);
        assert_eq!(lists.peers[0].name, "eng-r");
        assert_eq!(
            lists.peers[0].display_name.as_deref(),
            Some("Reviewer - backend PRs")
        );
        assert_eq!(lists.blocked[0].name, "eng-d");
        assert_eq!(lists.blocked[0].description, None);
    }

    #[test]
    fn a_new_agent_sends_only_what_was_typed() {
        let agent = NewAgent {
            name: "  eng-z ".into(),
            model: Some(" anthropic/claude-sonnet-4.5 ".into()),
            region: Some("   ".into()),
            ..Default::default()
        };
        let body = create_body(&agent).unwrap();
        assert_eq!(
            body,
            serde_json::json!({"name": "eng-z", "model": "anthropic/claude-sonnet-4.5"})
        );
    }

    #[test]
    fn a_new_agent_is_read_from_the_form_as_the_webview_sends_it() {
        let agent: NewAgent = serde_json::from_value(serde_json::json!({
            "name": "eng-z", "volumeGb": 3, "displayName": "Z", "model": "z-ai/glm-5.2"
        }))
        .unwrap();
        assert_eq!(agent.volume_gb, Some(3));
        assert_eq!(agent.display_name.as_deref(), Some("Z"));
        assert_eq!(agent.model.as_deref(), Some("z-ai/glm-5.2"));
    }

    #[test]
    fn a_new_agent_needs_a_name() {
        assert!(create_body(&NewAgent {
            name: "  ".into(),
            ..Default::default()
        })
        .is_err());
    }

    #[test]
    fn a_rotated_identity_is_read_without_a_token_in_it() {
        let body = r#"{"box_id":"b-1","name":"eng-g","expires_at":1797000000,
                       "scopes":["box:peer","git:credential"]}"#;
        let rotated: RotatedIdentity = serde_json::from_str(body).unwrap();
        assert_eq!(rotated.name, "eng-g");
        assert_eq!(rotated.expires_at, 1797000000.0);
        assert_eq!(rotated.scopes, vec!["box:peer", "git:credential"]);
    }

    #[test]
    fn nothing_blocked_is_an_empty_list_not_an_error() {
        // A control plane that omits `blocked` entirely is still readable.
        let lists = colleagues_from(r#"{"peers":[]}"#).unwrap();
        assert!(lists.peers.is_empty() && lists.blocked.is_empty());
    }

    #[test]
    fn errors_serialise_as_the_tagged_shape_the_frontend_matches_on() {
        // `isFleetError` in types.ts tests for a `kind` field, and the empty
        // state picks its message from it. If this ever serialised as
        // `{"Rejected": "..."}` — serde's default for an enum — all three
        // states would collapse into "unexpected" and the distinction the rest
        // of this module exists to draw would be silently gone.
        let json = serde_json::to_value(FleetError::Rejected("nope".into())).unwrap();
        assert_eq!(json["kind"], "rejected");
        assert_eq!(json["detail"], "nope");

        let json = serde_json::to_value(FleetError::NotConfigured("set a url".into())).unwrap();
        assert_eq!(json["kind"], "not_configured");
    }

    #[test]
    fn an_accepted_creation_reads_back_as_a_provisioning_box() {
        // The exact `202` body `_create_in_background` answers with. It is not
        // the `201` this used to get, and the difference is the bug: the row
        // is real, its status is `provisioning`, and there is no machine yet.
        // If this ever failed to parse, the app would report a creation that
        // succeeded as an unexpected answer.
        let body = r#"{"box":{"id":"b7","name":"eng-f","status":"provisioning",
                       "endpoint":null,"created_at":"2026-09-05T08:00:00+00:00",
                       "destroyed_at":null},"box_id":"b7","status":"provisioning"}"#;
        let row = box_from(body, "nope").expect("a 202 body must parse");
        assert_eq!(row.name, "eng-f");
        assert_eq!(row.status, "provisioning");
        assert!(row.endpoint.is_none(), "there is no machine yet");
    }

    #[test]
    fn a_bare_row_parses_too() {
        // The fallback for a body that is the row itself rather than `{"box":
        // …}`. Three endpoints wrap it today and nothing guarantees a fourth
        // will.
        let row = box_from(r#"{"id":"b1","name":"eng-a","status":"running"}"#, "nope").unwrap();
        assert_eq!(row.name, "eng-a");
    }

    #[test]
    fn a_repository_splits_into_two_path_segments() {
        // Revoke spells the repository as a path rather than a body, so this
        // decides which URL is called. A wrong split deletes something else or
        // nothing at all, and either reads as "revoke did not work".
        assert_eq!(
            split_repo("joaoh82/flotta").unwrap(),
            ("joaoh82".to_string(), "flotta".to_string())
        );
    }

    #[test]
    fn anything_that_is_not_owner_and_name_is_refused_rather_than_guessed() {
        // These reach `revoke` only from a list the control plane normalised,
        // so none of them should ever arrive — which is exactly why the
        // failure has to be loud rather than a URL built from a guess.
        for bad in ["flotta", "", "/", "a/b/c", "https://github.com/a/b"] {
            assert!(split_repo(bad).is_err(), "{bad:?} was accepted");
        }
    }

    #[test]
    fn an_empty_segment_does_not_silently_become_a_shorter_path() {
        // `a//b` has three segments, one empty. Filtering without counting
        // would turn it into `a/b` and revoke a repository nobody named.
        assert!(split_repo("a//b").is_err());
    }

    #[test]
    fn the_repository_list_is_read_from_the_answer_not_the_request() {
        // Every endpoint returns the whole list after the change, which is
        // what carries normalisation back: a pasted URL was granted as
        // `owner/name` and that is what must be displayed.
        let repos = repos_from(r#"{"box_id":"b1","repos":["joaoh82/flotta","a/b"]}"#).unwrap();
        assert_eq!(repos, vec!["joaoh82/flotta", "a/b"]);
    }

    #[test]
    fn an_agent_with_no_grants_is_an_empty_list_not_a_failure() {
        assert!(repos_from(r#"{"box_id":"b1","repos":[]}"#)
            .unwrap()
            .is_empty());
        // And a body that omits the key entirely — "none" is a real state.
        assert!(repos_from(r#"{"box_id":"b1"}"#).unwrap().is_empty());
    }

    #[test]
    fn a_timeline_keeps_the_word_type() {
        // `type` is a Rust keyword, so the field is `kind` — but the wire name
        // has to survive in *both* directions. The webview matches on
        // `torn_down` to decide whether an agent failed to be created, and it
        // reads that off `event.type`. A rename on the way out would leave the
        // app unable to tell a failure from a boot.
        let body = r#"{"events":[
            {"id":1,"entity_kind":"box","entity_id":"b7","ts":"2026-09-05T08:00:00+00:00",
             "type":"provisioning","payload":{"name":"eng-f","backend":"fly://"}},
            {"id":2,"entity_kind":"box","entity_id":"b7","ts":"2026-09-05T08:01:00+00:00",
             "type":"torn_down","payload":{"reason":"create failed: BackendError: no capacity"}}
        ]}"#;
        let events = serde_json::from_str::<EventList>(body)
            .expect("must parse")
            .events;
        assert_eq!(events.len(), 2);
        assert_eq!(events[1].kind, "torn_down");

        let out = serde_json::to_value(&events[1]).unwrap();
        assert_eq!(out["type"], "torn_down", "the frontend reads `type`");
        assert_eq!(out["entity_kind"], "box", "and it filters on this");
        assert_eq!(
            out["payload"]["reason"],
            "create failed: BackendError: no capacity"
        );
    }

    #[test]
    fn a_real_timeline_parses_to_the_events_it_contains() {
        // The literal body `GET /api/boxes/{id}/events` returned for a live
        // box, captured rather than hand-written — the same habit as
        // `fixture.json`, and for the same reason: the app decides whether an
        // agent failed to be created by reading this, and a hand-written
        // sample can only prove that the sample parses.
        //
        // It carries `entity_kind` and `entity_id`, which the app does not
        // model, and event types nothing here special-cases (`addressed`, the
        // door waking a box). Both must stay harmless.
        let body = include_str!("../tests/timeline.json");
        let events = serde_json::from_str::<EventList>(body)
            .expect("a real timeline must parse")
            .events;
        assert_eq!(events.len(), 18);
        assert_eq!(events[0].kind, "provisioning");
        assert!(events.iter().any(|e| e.kind == "addressed"));
        assert!(events.iter().all(|e| e.entity_kind == "box"));
        // The one field the failure panel actually reads out of a payload.
        assert!(events.iter().any(|e| e
            .payload
            .as_ref()
            .is_some_and(|p| p.get("reason").is_some())));
    }

    #[test]
    fn an_old_control_plane_says_so_rather_than_reading_as_a_broken_app() {
        // The app ships separately from the control plane, so a window running
        // ahead of its fleet is normal. "answered 404: Not Found" is true and
        // tells nobody what to do about it.
        match classify(404, "/api/settings", "") {
            Some(FleetError::Unexpected(detail)) => {
                assert!(detail.contains("older than the app"), "{detail}");
                assert!(
                    !detail.contains("404"),
                    "the status code is not the useful part"
                );
            }
            other => panic!("expected a named failure, got {other:?}"),
        }
    }

    #[test]
    fn a_404_about_an_agent_still_means_there_is_no_such_agent() {
        // The message above is per path on purpose: `/api/boxes/{id}` answering
        // 404 is correct and means something else entirely.
        match classify(404, "/api/boxes/b-nope", r#"{"detail":"no box 'b-nope'"}"#) {
            Some(FleetError::Unexpected(detail)) => assert!(detail.contains("no box"), "{detail}"),
            other => panic!("expected the control plane's own words, got {other:?}"),
        }
    }

    #[test]
    fn settings_carry_their_own_catalogue_so_the_form_is_not_hardcoded() {
        // The app renders label, help and default straight from this. If they
        // ever stopped arriving, the window would show a row of unlabelled
        // boxes named after environment variables — which is the thing this
        // whole feature exists to stop being the interface.
        let body = r#"{"settings":[
            {"key":"FLOTTA_IDLE_AFTER_S","label":"Sleep agents after",
             "help":"Seconds of quiet before an agent suspends itself.",
             "kind":"seconds","default":"1800","value":"300","source":"store"},
            {"key":"FLOTTA_MAX_CONCURRENT","label":"Live tasks at once",
             "help":"How much work may run at once.","kind":"int",
             "default":"1","value":"1","source":"default"}
        ]}"#;
        let settings = settings_from(body).expect("must parse");
        assert_eq!(settings.len(), 2);
        assert_eq!(settings[0].label, "Sleep agents after");
        assert_eq!(settings[0].source, "store");
        assert_eq!(settings[1].source, "default");
    }

    #[test]
    fn a_timeline_says_which_tier_each_event_belongs_to() {
        // `get_box_timeline` unions box, task and workspace events. Dropping
        // `entity_kind` made "why was this box torn down" mean "the last
        // `torn_down` from any tier", so a workspace teardown could be shown
        // as the agent's own fate. This is the field that stops it, and it has
        // to survive into the webview, where the filtering happens.
        let body = r#"{"events":[
            {"id":1,"entity_kind":"workspace","entity_id":"w1","ts":"t",
             "type":"torn_down","payload":{"reason":"box eng-f torn down"}},
            {"id":2,"entity_kind":"box","entity_id":"b7","ts":"t",
             "type":"torn_down","payload":{"reason":"create failed: no capacity"}}
        ]}"#;
        let events = serde_json::from_str::<EventList>(body)
            .expect("must parse")
            .events;
        assert_eq!(events[0].entity_kind, "workspace");
        assert_eq!(events[1].entity_kind, "box");
        let boxes: Vec<_> = events.iter().filter(|e| e.entity_kind == "box").collect();
        assert_eq!(boxes.len(), 1, "one of these two is the agent's own ending");
    }

    #[test]
    fn a_path_segment_cannot_change_which_endpoint_is_called() {
        // Both lookups take an id *or a name*, and a name is whatever
        // `store.create_box` was handed. A slash would address something else
        // entirely rather than fail.
        assert_eq!(encode_segment("b-98777076972d"), "b-98777076972d");
        assert_eq!(encode_segment("eng-a@b-1"), "eng-a%40b-1");
        assert_eq!(encode_segment("a/../b"), "a%2F..%2Fb");
        assert_eq!(encode_segment("two words"), "two%20words");
    }

    #[test]
    fn an_event_with_no_payload_still_parses() {
        // `add_event` takes the payload optionally and the column is nullable,
        // so a null is a real body and not a malformed one. Requiring it would
        // make the timeline unreadable the first time something recorded a
        // bare fact.
        let body = r#"{"events":[{"id":1,"entity_kind":"box","ts":"2026-09-05T08:00:00+00:00",
                       "type":"running","payload":null}]}"#;
        let events = serde_json::from_str::<EventList>(body)
            .expect("must parse")
            .events;
        assert!(events[0].payload.is_none() || events[0].payload.as_ref().unwrap().is_null());
    }

    // -- the machine panel -------------------------------------------------

    /// A real `GET /api/boxes/eng-g/machine` body, trimmed. Invented JSON
    /// would pin the shape I assumed rather than the one the control plane
    /// sends.
    const MACHINE_BODY: &str = r#"{
      "box": {"id": "b-79c6ca696e62", "name": "eng-g", "status": "stopped",
              "endpoint": "fly://joaoh82-flotta-eng-g/815990c9246728"},
      "machine": {"state": "started", "machine_id": "815990c9246728",
                  "app": "joaoh82-flotta-eng-g", "region": "ams",
                  "image": "registry.fly.io/joaoh82-flotta-images:deployment-01M2@sha256:bbaf",
                  "cpu_kind": "shared", "cpus": 1, "memory_mb": 1024,
                  "volume_id": "vol_vwnl2n2zqend82nv", "volume_gb": 2,
                  "volume_path": "/data", "private_ip": "fdaa:bd::2",
                  "created_at": "2026-09-08T21:00:43Z",
                  "updated_at": "2026-09-09T19:58:47Z", "host_status": "ok"},
      "unavailable": null
    }"#;

    #[test]
    fn a_machine_body_parses_whole() {
        let view: MachineView = serde_json::from_str(MACHINE_BODY).unwrap();
        let machine = view.machine.expect("a machine");

        assert_eq!(view.row.name, "eng-g");
        assert_eq!(machine.state, "started");
        assert_eq!(machine.region.as_deref(), Some("ams"));
        assert_eq!(machine.volume_id.as_deref(), Some("vol_vwnl2n2zqend82nv"));
        assert_eq!(machine.memory_mb, Some(1024));
        assert!(view.unavailable.is_none());
    }

    #[test]
    fn the_row_survives_a_machine_that_could_not_be_read() {
        // The panel must still render: without the row it has nothing to show
        // while the substrate is unreachable, which is when a person is most
        // likely to be looking at it.
        let body = r#"{"box": {"id": "b-1", "name": "eng-a", "status": "running"},
                       "machine": null,
                       "unavailable": "flyctl: not authenticated"}"#;
        let view: MachineView = serde_json::from_str(body).unwrap();

        assert!(view.machine.is_none());
        assert_eq!(
            view.unavailable.as_deref(),
            Some("flyctl: not authenticated")
        );
        assert_eq!(view.row.name, "eng-a");
    }

    #[test]
    fn a_machine_missing_every_optional_field_still_parses() {
        // `state` is the only thing the substrate always knows. Everything
        // else is one external tool's JSON, and a moved key must cost a blank
        // line in a panel rather than the whole read.
        let body = r#"{"box": {"id": "b-1", "name": "eng-a", "status": "running"},
                       "machine": {"state": "gone"}}"#;
        let view: MachineView = serde_json::from_str(body).unwrap();
        let machine = view.machine.expect("a machine");

        assert_eq!(machine.state, "gone");
        assert!(machine.image.is_none() && machine.cpus.is_none());
        assert!(view.unavailable.is_none());
    }

    #[test]
    fn an_old_control_plane_says_so_rather_than_answering_404() {
        // The app is on a laptop and the control plane is deployed elsewhere,
        // so a window running ahead of its fleet is normal. "answered 404: Not
        // Found" is true and reads as the app being broken.
        match classify(404, "/api/boxes/eng-a/machine", r#"{"detail":"Not Found"}"#) {
            Some(FleetError::Unexpected(detail)) => {
                assert!(detail.contains("older than the app"), "{detail}");
            }
            other => panic!("expected a version-skew message, got {other:?}"),
        }
    }

    #[test]
    fn a_missing_agent_is_not_reported_as_a_stale_deployment() {
        // The same status on the same path, meaning the opposite thing. Told
        // apart by the body, because nothing else distinguishes them.
        match classify(
            404,
            "/api/boxes/eng-q/machine",
            r#"{"detail":"no box 'eng-q'"}"#,
        ) {
            Some(FleetError::Unexpected(detail)) => assert_eq!(detail, "no box 'eng-q'"),
            other => panic!("expected the API's own reason, got {other:?}"),
        }
    }

    #[test]
    fn the_hermes_ref_and_the_image_verdict_parse() {
        let body = r#"{
          "box": {"id": "b-1", "name": "eng-b", "status": "stopped"},
          "machine": {"state": "suspended", "hermes_ref": "v2026.8.19"},
          "fleet_image": "registry.fly.io/images:deployment-01M2",
          "image_current": false
        }"#;
        let view: MachineView = serde_json::from_str(body).unwrap();

        assert_eq!(
            view.machine.unwrap().hermes_ref.as_deref(),
            Some("v2026.8.19")
        );
        assert_eq!(view.image_current, Some(false));
        assert!(view.fleet_image.is_some());
    }

    #[test]
    fn an_unknown_image_verdict_is_none_and_not_false() {
        // `false` means "behind", which puts an Upgrade button in front of
        // someone. Absent must not decay into it.
        let body = r#"{"box": {"id": "b-1", "name": "eng-b", "status": "stopped"},
                       "machine": {"state": "suspended"}}"#;
        let view: MachineView = serde_json::from_str(body).unwrap();

        assert_eq!(view.image_current, None);
        assert!(view.machine.unwrap().hermes_ref.is_none());
    }

    #[test]
    fn hermes_versions_parse_including_a_failed_check() {
        let ok: HermesVersions = serde_json::from_str(
            r#"{"pinned":"v2026.8.19","latest":"v2026.9.7","behind":true,"fleet_image":"r/x:t"}"#,
        )
        .unwrap();
        assert!(ok.behind && ok.latest.as_deref() == Some("v2026.9.7"));

        // The one that matters: unreachable GitHub is unknown, not up to date.
        let down: HermesVersions = serde_json::from_str(
            r#"{"pinned":"v2026.8.19","latest":null,"behind":false,
                "unavailable":"could not reach GitHub"}"#,
        )
        .unwrap();
        assert!(!down.behind);
        assert!(down.latest.is_none());
        assert!(down.unavailable.is_some());
    }

    #[test]
    fn identity_fields_are_optional_and_default_to_none() {
        // An older control plane, or an agent nobody has named: the row must
        // still parse, and the window falls back to the address.
        let bare: BoxRow =
            serde_json::from_str(r#"{"id":"b-1","name":"eng-a","status":"stopped"}"#).unwrap();
        assert!(bare.display_name.is_none() && bare.description.is_none());

        let named: BoxRow = serde_json::from_str(
            r#"{"id":"b-1","name":"eng-a","status":"stopped",
                "display_name":"Reviewer","description":"Reviews PRs","instructions":"Be kind."}"#,
        )
        .unwrap();
        assert_eq!(named.display_name.as_deref(), Some("Reviewer"));
        assert_eq!(named.instructions.as_deref(), Some("Be kind."));
        assert_eq!(
            named.name, "eng-a",
            "the address is untouched by a display name"
        );
    }
}
