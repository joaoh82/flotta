//! Talking to a box, through the door.
//!
//! The protocol is Hermes's, decoded in `src/flotta/client.py` and confirmed
//! against a live box. This is the same conversation in Rust, and the same
//! three things that are easy to get wrong:
//!
//! 1. **`prompt.submit`'s response is only an acknowledgement.** The reply
//!    arrives later as a `message.complete` *event*. Waiting on the RPC result
//!    waits forever.
//! 2. **A provider failure arrives as a normal `message.complete`** whose text
//!    is an error string and whose `status` is not `complete`. Rendering it as
//!    a reply puts "No inference provider configured" in the transcript as if
//!    the agent had said it.
//! 3. **The ws ticket is single-use and expires in about 30 seconds.** It is
//!    minted per connection; caching one fails on the next reconnect.
//!
//! ## Why the connection lives here and not in the webview
//!
//! Same reason as `fleet`, with one addition: the token travels to the door as
//! `?access_token=` on the WebSocket URL, because a browser cannot set headers
//! on a handshake. Doing that from JavaScript would put the token in the
//! webview *and* in a URL. From here it is neither.

use crate::fleet::{FleetError, Settings};
use futures_util::{SinkExt, StreamExt};
use serde::Serialize;
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::Duration;
use tauri::{Emitter, Manager};
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::Message;

/// Hermes's login provider. A form, not HTTP Basic — which is why the door
/// rewrites the request body rather than attaching a header.
const PROVIDER: &str = "basic";

/// Well inside Cloudflare's idle-WebSocket cut-off, which is unpublished and
/// reported at roughly 100 seconds. A silent close mid-conversation is
/// indistinguishable from the agent thinking, so it is worth a ping.
const PING_EVERY: Duration = Duration::from_secs(30);

/// A cold box takes 10-60s to wake: the door resolves it, starts the machine
/// and waits for Hermes to import itself. That is not a hang, and the first
/// request of the day always pays it.
const WAKE_TIMEOUT: Duration = Duration::from_secs(180);

/// Bounds a model call, not a round trip.
const TURN_TIMEOUT: Duration = Duration::from_secs(300);

static RPC_ID: AtomicU64 = AtomicU64::new(1);

/// What the UI is told, as it happens.
///
/// A tagged union for the same reason `FleetError` is: "waking", "thinking"
/// and "failed" are different states and a UI that cannot tell them apart
/// shows a spinner for all three — which reads as a hang precisely when the
/// box genuinely is not there yet.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum AgentEvent {
    /// Connecting, and why it might be slow.
    Waking { box_name: String },
    /// The socket is open and a session exists.
    Ready { box_name: String },
    /// A turn is in flight.
    Thinking { box_name: String },
    /// The agent answered.
    Reply { box_name: String, text: String },
    /// Something went wrong. Ends the conversation.
    Failed { box_name: String, detail: String },
    /// The conversation closed cleanly.
    Closed { box_name: String },
}

impl AgentEvent {
    fn emit(self, app: &tauri::AppHandle) {
        // A failed emit means the window is gone, which is not worth
        // propagating: there is nobody left to tell.
        let _ = app.emit("agent://event", self);
    }
}

/// One live conversation per box, keyed by name.
///
/// Lives here rather than in `lib.rs` because the task is what makes an entry
/// live or dead, and the task has to be able to remove its own: when a
/// conversation ends, an entry left behind is a sender nobody is listening to,
/// and the next `open_conversation` succeeds against it while nothing happens.
#[derive(Default)]
pub struct Conversations(pub Mutex<HashMap<String, mpsc::Sender<String>>>);

impl Conversations {
    /// The sender for a box, if there is a task still reading from it.
    ///
    /// `is_closed` is the honest test: it becomes true the moment the receiver
    /// drops, which is the moment the task returns. Checking only for the
    /// key's presence treats a finished conversation as a live one.
    pub fn live(&self, box_name: &str) -> Option<mpsc::Sender<String>> {
        let mut map = self.0.lock().unwrap();
        match map.get(box_name) {
            Some(sender) if !sender.is_closed() => Some(sender.clone()),
            Some(_) => {
                map.remove(box_name);
                None
            }
            None => None,
        }
    }

    pub fn insert(&self, box_name: String, sender: mpsc::Sender<String>) {
        self.0.lock().unwrap().insert(box_name, sender);
    }

    pub fn forget(&self, box_name: &str) {
        self.0.lock().unwrap().remove(box_name);
    }
}

/// Tell the UI a conversation is already up.
///
/// `Conversation` resets to "waking" whenever it mounts, so re-selecting an
/// agent whose socket is already open left it waiting for an event that would
/// never come again — stuck on "Waking…" with the composer disabled, for a
/// connection that was working the whole time.
pub fn announce_ready(app: &tauri::AppHandle, box_name: &str) {
    AgentEvent::Ready {
        box_name: box_name.to_string(),
    }
    .emit(app);
}

pub fn door_url(settings: &Settings, box_name: &str) -> String {
    let domain = settings.domain.trim().trim_matches('.');
    let domain = if domain.is_empty() {
        "flotta.dev"
    } else {
        domain
    };
    format!("https://{box_name}.{domain}")
}

/// Log in, mint a ticket, and open the socket. Returns the live socket and the
/// session id to submit prompts against.
async fn connect(
    settings: &Settings,
    box_name: &str,
    token: &str,
) -> Result<
    (
        tokio_tungstenite::WebSocketStream<
            tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
        >,
        String,
    ),
    FleetError,
> {
    let base = door_url(settings, box_name);
    let jar = std::sync::Arc::new(reqwest::cookie::Jar::default());
    let http = reqwest::Client::builder()
        .timeout(WAKE_TIMEOUT)
        .cookie_provider(jar.clone())
        .build()
        .map_err(|e| FleetError::Unexpected(e.to_string()))?;

    // The door fills in the box's real username and password on the way
    // through, so neither ever exists on this machine. Empty strings here are
    // correct, not a placeholder.
    let login = http
        .post(format!("{base}/auth/password-login"))
        .bearer_auth(token)
        .json(&serde_json::json!({
            "provider": PROVIDER, "username": "", "password": "", "next": ""
        }))
        .send()
        .await
        .map_err(|e| FleetError::Unreachable(format!("could not reach {base}: {e}")))?;

    if !login.status().is_success() {
        let status = login.status().as_u16();
        let body = login.text().await.unwrap_or_default();
        return Err(if status == 401 || status == 403 {
            FleetError::Rejected(format!(
                "the door refused this token for {box_name} ({status}). It needs \
                 the `box:chat` scope. {}",
                body.chars().take(200).collect::<String>()
            ))
        } else {
            FleetError::Unexpected(format!(
                "logging in to {box_name} failed ({status}): {}",
                body.chars().take(200).collect::<String>()
            ))
        });
    }

    let ticket_response = http
        .post(format!("{base}/api/auth/ws-ticket"))
        .bearer_auth(token)
        .send()
        .await
        .map_err(|e| FleetError::Unreachable(format!("{base} stopped answering: {e}")))?;
    if !ticket_response.status().is_success() {
        return Err(FleetError::Unexpected(format!(
            "could not mint a ws ticket ({}). A 401 here after a successful \
             login means the session cookie was dropped.",
            ticket_response.status()
        )));
    }
    let ticket = ticket_response
        .json::<serde_json::Value>()
        .await
        .ok()
        .and_then(|v| v.get("ticket")?.as_str().map(str::to_owned))
        .ok_or_else(|| FleetError::Unexpected("ws-ticket returned no ticket".into()))?;

    // The token goes in the query string because a WebSocket handshake cannot
    // carry headers in a browser — the door accepts it there and strips it
    // before proxying. Sending it the same way keeps one path tested.
    //
    // Built with the URL type rather than `format!`: a ticket is minted by the
    // box and a token by whoever ran `flotta token mint`, so neither is ours
    // to assume is query-safe. One `&` or `+` in either and the handshake
    // fails with an error about the *other* parameter.
    let mut ws_url = reqwest::Url::parse(&base.replacen("https://", "wss://", 1))
        .map_err(|e| FleetError::Unexpected(format!("bad door url: {e}")))?;
    ws_url.set_path("/api/ws");
    ws_url
        .query_pairs_mut()
        .append_pair("ticket", &ticket)
        .append_pair("access_token", token);
    let ws_url = ws_url.to_string();

    // The session cookie has to come along by hand: the WebSocket client does
    // not share the HTTP client's jar, and the box's ticket check expects the
    // logged-in session.
    let mut request = ws_url
        .into_client_request()
        .map_err(|e| FleetError::Unexpected(format!("bad agent socket url: {e}")))?;
    if let Some(cookie) = reqwest::cookie::CookieStore::cookies(
        jar.as_ref(),
        &base.parse().expect("door url parsed once already"),
    ) {
        request
            .headers_mut()
            .insert(reqwest::header::COOKIE, cookie);
    }

    let (mut socket, _) = tokio_tungstenite::connect_async(request)
        .await
        .map_err(|e| FleetError::Unreachable(format!("agent socket refused: {e}")))?;

    // The server speaks first.
    expect_event(&mut socket, "gateway.ready").await?;

    let session_id = create_session(&mut socket).await?;
    Ok((socket, session_id))
}

type Socket =
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

async fn next_frame(
    socket: &mut Socket,
    within: Duration,
) -> Result<serde_json::Value, FleetError> {
    loop {
        let message = tokio::time::timeout(within, socket.next())
            .await
            .map_err(|_| FleetError::Unreachable("the agent stopped responding".into()))?
            .ok_or_else(|| FleetError::Unreachable("the agent socket closed".into()))?
            .map_err(|e| FleetError::Unreachable(format!("agent socket error: {e}")))?;

        match message {
            Message::Text(text) => {
                return serde_json::from_str(&text).map_err(|e| {
                    FleetError::Unexpected(format!(
                        "the agent sent something that is not JSON: {e}"
                    ))
                })
            }
            // Pongs and pings are the keep-alive, not content.
            Message::Ping(_) | Message::Pong(_) => continue,
            Message::Close(_) => {
                return Err(FleetError::Unreachable(
                    "the agent closed the socket".into(),
                ))
            }
            _ => continue,
        }
    }
}

async fn expect_event(socket: &mut Socket, wanted: &str) -> Result<(), FleetError> {
    let frame = next_frame(socket, WAKE_TIMEOUT).await?;
    let kind = frame
        .get("params")
        .and_then(|p| p.get("type"))
        .and_then(|t| t.as_str());
    if kind == Some(wanted) {
        Ok(())
    } else {
        Err(FleetError::Unexpected(format!(
            "expected {wanted} from the agent, got {kind:?}"
        )))
    }
}

async fn rpc(
    socket: &mut Socket,
    method: &str,
    params: serde_json::Value,
) -> Result<u64, FleetError> {
    let id = RPC_ID.fetch_add(1, Ordering::Relaxed);
    let body = serde_json::json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params});
    socket
        .send(Message::Text(body.to_string()))
        .await
        .map_err(|e| FleetError::Unreachable(format!("could not send {method}: {e}")))?;
    Ok(id)
}

async fn create_session(socket: &mut Socket) -> Result<String, FleetError> {
    let id = rpc(socket, "session.create", serde_json::json!({})).await?;
    // A wall-clock deadline, not a per-frame one: events interleave with
    // responses, and resetting the clock on every ignored event means a chatty
    // gateway that never answers keeps the handshake alive forever.
    let deadline = tokio::time::Instant::now() + WAKE_TIMEOUT;
    loop {
        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        if remaining.is_zero() {
            return Err(FleetError::Unreachable(
                "the agent never opened a session".into(),
            ));
        }
        let frame = next_frame(socket, remaining).await?;
        if frame.get("id").and_then(|v| v.as_u64()) != Some(id) {
            continue;
        }
        if let Some(error) = frame.get("error") {
            return Err(FleetError::Unexpected(format!(
                "the agent refused to open a session: {error}"
            )));
        }
        return frame
            .get("result")
            .and_then(|r| r.get("session_id"))
            .and_then(|s| s.as_str())
            .map(str::to_owned)
            .ok_or_else(|| FleetError::Unexpected("session.create returned no id".into()));
    }
}

/// Submit one prompt and wait for the agent's answer.
async fn one_turn(socket: &mut Socket, session_id: &str, text: &str) -> Result<String, FleetError> {
    let id = rpc(
        socket,
        "prompt.submit",
        serde_json::json!({"session_id": session_id, "text": text}),
    )
    .await?;

    let deadline = tokio::time::Instant::now() + TURN_TIMEOUT;
    loop {
        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        if remaining.is_zero() {
            return Err(FleetError::Unreachable(
                "the agent did not answer in time".into(),
            ));
        }
        let frame = next_frame(socket, remaining).await?;

        match interpret(&frame, id) {
            Verdict::Ignore => continue,
            Verdict::Reply(text) => return Ok(text),
            Verdict::Failed(err) => return Err(err),
        }
    }
}

/// What one frame means, mid-turn.
///
/// Split out of the loop so the three traps can be tested without a socket.
/// They are the reason this is a loop over frames rather than an await on a
/// call, and until now the only thing asserting them was prose.
#[derive(Debug)]
enum Verdict {
    /// Not about this turn. Events interleave with responses constantly.
    Ignore,
    Reply(String),
    Failed(FleetError),
}

fn interpret(frame: &serde_json::Value, id: u64) -> Verdict {
    // A refused submit — unknown session, bad params — comes back as a
    // JSON-RPC error against our id, never as a completion. Treated as noise
    // it would burn the whole turn deadline waiting for an answer that was
    // refused in milliseconds.
    if frame.get("id").and_then(|v| v.as_u64()) == Some(id) {
        return match frame.get("error") {
            Some(error) => Verdict::Failed(FleetError::Unexpected(format!(
                "the agent refused the message: {error}"
            ))),
            // The bare acknowledgement. The reply is still coming, as an event.
            None => Verdict::Ignore,
        };
    }

    let Some(params) = frame.get("params") else {
        return Verdict::Ignore;
    };
    if params.get("type").and_then(|t| t.as_str()) != Some("message.complete") {
        return Verdict::Ignore;
    }

    let payload = params.get("payload").cloned().unwrap_or_default();
    let reply = payload
        .get("text")
        .and_then(|t| t.as_str())
        .unwrap_or_default()
        .to_string();

    // The trap worth naming twice: a provider failure is a *successful*
    // completion carrying an error string. Returning it as a reply prints
    // "No inference provider configured" as though the agent had said it.
    match payload.get("status").and_then(|s| s.as_str()) {
        Some(status) if status != "complete" => Verdict::Failed(FleetError::Unexpected(format!(
            "the agent could not answer ({status}): {}",
            reply.chars().take(300).collect::<String>()
        ))),
        _ => Verdict::Reply(reply),
    }
}

/// End a conversation: tell the UI, and stop claiming it is open.
///
/// Every exit path goes through here. Without the `forget`, a failed
/// conversation left its sender in the map, `open_conversation` saw a key and
/// returned early, and the user could not reconnect to that agent without
/// restarting the app — a dead entry is worse than no entry, because it
/// silently absorbs every attempt to fix it.
fn finish(app: &tauri::AppHandle, box_name: &str, failure: Option<String>) {
    if let Some(state) = app.try_state::<Conversations>() {
        state.forget(box_name);
    }
    match failure {
        Some(detail) => AgentEvent::Failed {
            box_name: box_name.to_string(),
            detail,
        },
        None => AgentEvent::Closed {
            box_name: box_name.to_string(),
        },
    }
    .emit(app);
}

/// Own one conversation for as long as the UI wants it.
///
/// A task rather than a command per turn, because the socket has to outlive a
/// single call: reconnecting per message would re-pay the login, the ticket
/// and — if the box has gone back to sleep — the wake.
pub async fn run(
    app: tauri::AppHandle,
    settings: Settings,
    box_name: String,
    token: String,
    mut prompts: mpsc::Receiver<String>,
) {
    AgentEvent::Waking {
        box_name: box_name.clone(),
    }
    .emit(&app);

    let (mut socket, session_id) = match connect(&settings, &box_name, &token).await {
        Ok(pair) => pair,
        Err(err) => {
            finish(&app, &box_name, Some(err.detail().to_string()));
            return;
        }
    };

    AgentEvent::Ready {
        box_name: box_name.clone(),
    }
    .emit(&app);

    let mut ping = tokio::time::interval(PING_EVERY);
    ping.tick().await; // the first tick is immediate

    loop {
        tokio::select! {
            prompt = prompts.recv() => {
                let Some(prompt) = prompt else {
                    finish(&app, &box_name, None);
                    return;
                };
                AgentEvent::Thinking { box_name: box_name.clone() }.emit(&app);
                match one_turn(&mut socket, &session_id, &prompt).await {
                    Ok(text) => AgentEvent::Reply { box_name: box_name.clone(), text }.emit(&app),
                    Err(err) => {
                        finish(&app, &box_name, Some(err.detail().to_string()));
                        return;
                    }
                }
            }
            _ = ping.tick() => {
                // Cloudflare closes an idle proxied socket without telling
                // either end, and the symptom is a reply that never arrives.
                if socket.send(Message::Ping(Vec::new())).await.is_err() {
                    finish(&app, &box_name, Some("the connection to the agent dropped".into()));
                    return;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_door_url_is_the_boxs_public_address() {
        let settings = Settings {
            control_url: String::new(),
            domain: "flotta.dev".into(),
        };
        assert_eq!(door_url(&settings, "eng-a"), "https://eng-a.flotta.dev");
    }

    #[test]
    fn a_missing_domain_falls_back_rather_than_building_a_broken_host() {
        // `https://eng-a.` is a hostname that cannot resolve, and the failure
        // would surface as "agent socket refused" — a network error for what
        // is really an unset setting.
        let settings = Settings::default();
        assert_eq!(door_url(&settings, "eng-a"), "https://eng-a.flotta.dev");
    }

    #[test]
    fn a_domain_with_stray_dots_still_produces_one_separator() {
        let settings = Settings {
            control_url: String::new(),
            domain: ".flotta.dev.".into(),
        };
        assert_eq!(door_url(&settings, "eng-a"), "https://eng-a.flotta.dev");
    }

    #[test]
    fn a_completion_is_the_reply() {
        let v = interpret(
            &serde_json::json!({
                "params": {"type": "message.complete",
                           "payload": {"text": "pong", "status": "complete"}}
            }),
            7,
        );
        assert!(matches!(v, Verdict::Reply(t) if t == "pong"));
    }

    #[test]
    fn a_provider_failure_is_not_the_agent_talking() {
        // It arrives as a *successful* completion whose status is not
        // `complete`. Rendered as a reply, "No inference provider configured"
        // appears in the transcript as though the agent had said it — wrong,
        // and unactionable for whoever reads it.
        let v = interpret(
            &serde_json::json!({
                "params": {"type": "message.complete",
                           "payload": {"text": "No inference provider configured",
                                       "status": "error"}}
            }),
            7,
        );
        match v {
            Verdict::Failed(err) => {
                assert!(
                    err.detail().contains("could not answer"),
                    "{}",
                    err.detail()
                )
            }
            other => panic!("expected a failure, got {other:?}"),
        }
    }

    #[test]
    fn a_refused_submit_ends_the_turn_immediately() {
        // Against our own id, and never as a completion. Ignored, it would
        // cost the full turn deadline for an answer refused in milliseconds.
        let v = interpret(
            &serde_json::json!({"id": 7, "error": {"message": "unknown session"}}),
            7,
        );
        match v {
            Verdict::Failed(err) => assert!(err.detail().contains("unknown session")),
            other => panic!("expected a failure, got {other:?}"),
        }
    }

    #[test]
    fn the_acknowledgement_is_not_the_reply() {
        // `prompt.submit` answers immediately with a bare result. Treating it
        // as the reply returns an empty message before the agent has thought.
        let v = interpret(&serde_json::json!({"id": 7, "result": {}}), 7);
        assert!(matches!(v, Verdict::Ignore));
    }

    #[test]
    fn another_turns_frames_are_ignored() {
        let v = interpret(
            &serde_json::json!({"id": 99, "error": {"message": "not ours"}}),
            7,
        );
        assert!(matches!(v, Verdict::Ignore));
    }

    #[test]
    fn interleaved_events_are_ignored() {
        for kind in ["message.delta", "gateway.ready", "session.updated"] {
            let v = interpret(&serde_json::json!({"params": {"type": kind}}), 7);
            assert!(matches!(v, Verdict::Ignore), "{kind} should be ignored");
        }
    }

    /// A real conversation with a real box. **Ignored by default** — it costs
    /// a machine wake and one model call, and the suite's promise is that it
    /// is hermetic and free.
    ///
    /// Run it by hand when the protocol changes, which is the only time it
    /// earns its cost:
    ///
    /// ```sh
    /// FLOTTA_TOKEN=$(uv run flotta token mint you --scope box:chat) \
    ///   cargo test --manifest-path app/src-tauri/Cargo.toml -- --ignored --nocapture
    /// ```
    #[test]
    #[ignore = "talks to a real box: costs a wake and a model call"]
    fn a_real_box_answers() {
        let Ok(token) = std::env::var("FLOTTA_TOKEN") else {
            panic!("set FLOTTA_TOKEN to a box:chat token");
        };
        let settings = Settings {
            control_url: String::new(),
            domain: std::env::var("FLOTTA_DOMAIN").unwrap_or_else(|_| "flotta.dev".into()),
        };
        let box_name = std::env::var("FLOTTA_BOX").unwrap_or_else(|_| "eng-a".into());

        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();

        runtime.block_on(async {
            let (mut socket, session) = connect(&settings, &box_name, &token)
                .await
                .unwrap_or_else(|e| panic!("connect failed: {}", e.detail()));
            println!("connected, session {session}");

            let reply = one_turn(
                &mut socket,
                &session,
                "Reply with exactly one word: pong. Nothing else.",
            )
            .await
            .unwrap_or_else(|e| panic!("turn failed: {}", e.detail()));

            println!("reply: {reply:?}");
            assert!(!reply.trim().is_empty(), "the agent replied with nothing");
        });
    }
}
