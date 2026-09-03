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
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;
use tauri::Emitter;
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
    let ws_url = format!(
        "{}/api/ws?ticket={ticket}&access_token={token}",
        base.replacen("https://", "wss://", 1)
    );

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

        // A refused submit — unknown session, bad params — comes back as a
        // JSON-RPC error against our id, never as a completion. Treating it as
        // noise means waiting out the whole turn deadline for an answer that
        // was refused in milliseconds.
        if frame.get("id").and_then(|v| v.as_u64()) == Some(id) {
            if let Some(error) = frame.get("error") {
                return Err(FleetError::Unexpected(format!(
                    "the agent refused: {error}"
                )));
            }
            continue;
        }

        let params = match frame.get("params") {
            Some(p) => p,
            None => continue,
        };
        if params.get("type").and_then(|t| t.as_str()) != Some("message.complete") {
            continue;
        }
        let payload = params.get("payload").cloned().unwrap_or_default();
        let reply = payload
            .get("text")
            .and_then(|t| t.as_str())
            .unwrap_or_default()
            .to_string();
        let status = payload.get("status").and_then(|s| s.as_str());

        // The trap worth naming twice: a provider failure is a *successful*
        // completion carrying an error string. Returning it as a reply prints
        // "No inference provider configured" as though the agent had said it.
        if let Some(status) = status {
            if status != "complete" {
                return Err(FleetError::Unexpected(format!(
                    "the agent could not answer ({status}): {}",
                    reply.chars().take(300).collect::<String>()
                )));
            }
        }
        return Ok(reply);
    }
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
            AgentEvent::Failed {
                box_name,
                detail: err.detail().to_string(),
            }
            .emit(&app);
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
                    AgentEvent::Closed { box_name }.emit(&app);
                    return;
                };
                AgentEvent::Thinking { box_name: box_name.clone() }.emit(&app);
                match one_turn(&mut socket, &session_id, &prompt).await {
                    Ok(text) => AgentEvent::Reply { box_name: box_name.clone(), text }.emit(&app),
                    Err(err) => {
                        AgentEvent::Failed {
                            box_name,
                            detail: err.detail().to_string(),
                        }
                        .emit(&app);
                        return;
                    }
                }
            }
            _ = ping.tick() => {
                // Cloudflare closes an idle proxied socket without telling
                // either end, and the symptom is a reply that never arrives.
                if socket.send(Message::Ping(Vec::new())).await.is_err() {
                    AgentEvent::Failed {
                        box_name,
                        detail: "the connection to the agent dropped".into(),
                    }
                    .emit(&app);
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
