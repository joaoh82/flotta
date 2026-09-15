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
use std::collections::{HashMap, VecDeque};
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
    /// The socket is open and a session exists. `resumed` is the conversation
    /// the box already had — empty for a new one.
    ///
    /// The transcript arrives from the box rather than being kept in the UI,
    /// which is the honest shape: the agent's memory is the source of truth,
    /// and a client that cached it would show its own copy after the agent had
    /// moved on.
    Ready {
        box_name: String,
        resumed: Vec<HistoryLine>,
    },
    /// The conversation was started over, and the transcript is now empty.
    ///
    /// Distinct from `Ready` with an empty `resumed`, which the window
    /// deliberately ignores: an empty `Ready` also arrives from a resync that
    /// found nothing yet, and treating that as "clear the screen" wiped the
    /// transcript of an agent mid-conversation. One event means "there is
    /// nothing to show yet", the other means "there is nothing any more", and
    /// they are not the same instruction.
    Reset { box_name: String },
    /// The agent wants to run something Hermes considers risky, and is
    /// **waiting for a person to decide**.
    ///
    /// Before this existed the app ignored `approval.request` entirely, so a
    /// turn that tripped Hermes's approval gate sat on "thinking…" until the
    /// gate's own timeout denied it — an agent blocked on a question the
    /// window never asked. `choices` are Hermes's, not ours: a command its
    /// smart classifier already judged dangerous is offered fewer of them.
    Approval {
        box_name: String,
        request: ApprovalRequest,
    },
    /// A turn is in flight.
    Thinking { box_name: String },
    /// What the agent is doing, while it does it (FLOTTA-61).
    ///
    /// Before this the window showed "thinking…" from submit to reply, so a
    /// nine-step turn that ran seven shell commands looked identical to one
    /// that had hung. Hermes was reporting every step the whole time; the app
    /// was throwing it away and waiting for `message.complete`.
    Progress { box_name: String, step: Step },
    /// The agent answered.
    Reply { box_name: String, text: String },
    /// Something went wrong. Ends the conversation.
    Failed { box_name: String, detail: String },
    /// The conversation closed cleanly.
    Closed { box_name: String },
}

/// One line of a conversation the box already had.
#[derive(Debug, Clone, Serialize, serde::Deserialize)]
pub struct HistoryLine {
    pub role: String,
    pub text: String,
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
pub struct Conversations(pub Mutex<HashMap<String, mpsc::Sender<Command>>>);

/// What the UI asks a live conversation to do.
#[derive(Debug)]
pub enum Command {
    /// Send this to the agent.
    Prompt(String),
    /// Re-announce readiness *with the transcript*.
    ///
    /// Selecting an agent remounts its view, which clears the transcript —
    /// the component is keyed by name so one agent's words can never appear
    /// under another's. So returning to a conversation that is still open has
    /// to be told the history again, and `Ready` with an empty `resumed` left
    /// the pane reading "eng-d is listening" about an agent mid-conversation.
    ///
    /// It re-reads from the box rather than replaying a cache: the agent's
    /// memory is the source of truth, and this is the one place a cache would
    /// silently diverge from it.
    Resync,
    /// Start the conversation over.
    ///
    /// **A reset is a new session, because nothing else can be.** Hermes
    /// renders an agent's system prompt once, when a session starts, and
    /// stores it keyed by hash; the upsert that writes it keeps the first hash
    /// a session ever gets. So the standing instructions on the volume reach a
    /// running agent only across a session boundary, and there is no
    /// "reload SOUL.md" — the frozen prefix is what makes provider caching
    /// work, and re-rendering it mid-conversation would re-bill every turn.
    ///
    /// The old transcript is **not** deleted. It stays on the box's volume
    /// while `session.most_recent` answers with the newest, so reopening the
    /// agent lands on the fresh one. `session.delete` exists and is
    /// deliberately not used: forgetting a conversation is not what "start
    /// over" should mean.
    Reset,
    /// Answer a pending approval.
    ///
    /// Only meaningful **during** a turn — that is the only time Hermes is
    /// waiting on one — so it is read inside `one_turn` rather than the idle
    /// loop. Arriving between turns it is dropped: whatever it answered has
    /// already been decided, by the gate's timeout if nothing else.
    Approve {
        request_id: Option<String>,
        choice: String,
    },
}

/// What Hermes asked permission for.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ApprovalRequest {
    /// Absent on older gateways, which resolve the oldest pending approval
    /// instead. Sent back when present so two approvals in one turn cannot
    /// be answered in the wrong order.
    pub request_id: Option<String>,
    /// Already redacted by the gateway: a credential-shaped value in the
    /// command is masked before it leaves the box.
    pub command: String,
    /// Hermes's own one-line reason it stopped to ask.
    pub description: String,
    /// The category Hermes matched, e.g. `delete in root path`.
    ///
    /// **This, not the command, is what an allow beyond "once" grants.**
    /// Hermes records approvals by pattern, so allowing one temp-directory
    /// delete for the conversation allows every command it classifies the same
    /// way. Carried so the window can say that before the click (FLOTTA-62).
    pub pattern: Option<String>,
    pub choices: Vec<String>,
}

/// One step of a turn in progress.
///
/// The payload shapes these are read from were captured off a live gateway
/// (v2026.9.11) rather than taken from `vendor/`, the same way the approval
/// payload was — field names are only trustworthy once seen on the wire:
///
/// ```text
/// tool.generating  {"name":"terminal"}
/// tool.start       {"tool_id":"call_…","name":"terminal","context":"ls /workspace","args":{…}}
/// tool.complete    {"tool_id":"call_…","name":"terminal","duration_s":0.24,
///                   "result":{"error":null,"exit_code":0,"output":"…"},"args":{…}}
/// reasoning.delta  {"text":" user wants me to"}
/// message.delta    {"text":"` is empty,"}
/// ```
///
/// Not carried: `thinking.delta`, which is Hermes's terminal spinner
/// (`"◉_◉ processing..."`) rather than anything the agent said, and a tool's
/// output, which can be megabytes and is the agent's to summarise.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Step {
    /// The model is reasoning; `text` is the next piece of it.
    Reasoning { text: String },
    /// The model is writing a call to this tool. Precedes `ToolStarted`, often
    /// by the longest silence in a step.
    Preparing { tool: String },
    /// The next piece of what the agent is saying.
    Text { text: String },
    /// Something the agent said **before** using a tool — "let me check" — now
    /// finished. The final reply does not repeat it, so it is a line of the
    /// transcript in its own right rather than part of the streaming answer.
    Said { text: String },
    ToolStarted {
        id: String,
        tool: String,
        /// One line, bounded: what the tool was asked to do.
        detail: String,
    },
    ToolFinished {
        id: String,
        tool: String,
        seconds: Option<f64>,
        failed: bool,
    },
}

/// Longest `detail` sent to the window. A tool's context can be a whole file
/// being written; the step is a label, not a viewer.
const DETAIL_CHARS: usize = 160;

/// A tool's context as one bounded line.
fn one_line(text: &str) -> String {
    let flat = text.split_whitespace().collect::<Vec<_>>().join(" ");
    if flat.chars().count() <= DETAIL_CHARS {
        flat
    } else {
        let cut: String = flat.chars().take(DETAIL_CHARS - 1).collect();
        format!("{cut}…")
    }
}

/// A progress event's payload, as a step — or nothing, for events that are
/// not progress or carry nothing worth showing.
///
/// Tolerant like `read_approval`: a missing field degrades the label, it never
/// drops the frame into an error. A step the window cannot fully describe is
/// still better than a return to a bare "thinking…".
fn read_step(kind: &str, payload: &serde_json::Value) -> Option<Step> {
    let text = |key: &str| {
        payload
            .get(key)
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string()
    };
    match kind {
        "reasoning.delta" => Some(text("text"))
            .filter(|t| !t.is_empty())
            .map(|text| Step::Reasoning { text }),
        "message.delta" => Some(text("text"))
            .filter(|t| !t.is_empty())
            .map(|text| Step::Text { text }),
        "tool.generating" => Some(Step::Preparing { tool: text("name") }),
        "tool.start" => Some(Step::ToolStarted {
            id: text("tool_id"),
            tool: text("name"),
            detail: one_line(&text("context")),
        }),
        "tool.complete" => {
            let result = payload.get("result");
            // A tool reports failure two ways: an `error` string (the tool
            // itself broke) or a non-zero `exit_code` (the command ran and
            // failed). eng-r's slow turn was three of the second kind, each
            // indistinguishable from success on screen.
            let errored = result
                .and_then(|r| r.get("error"))
                .and_then(|e| e.as_str())
                .is_some_and(|e| !e.is_empty());
            let exited = result
                .and_then(|r| r.get("exit_code"))
                .and_then(|c| c.as_i64())
                .is_some_and(|c| c != 0);
            Some(Step::ToolFinished {
                id: text("tool_id"),
                tool: text("name"),
                seconds: payload.get("duration_s").and_then(|d| d.as_f64()),
                failed: errored || exited,
            })
        }
        _ => None,
    }
}

/// What a turn has shown so far, so a pane that remounts mid-turn can be shown
/// it again.
///
/// Also where narration becomes a line of its own. Text streams as `Text`, but
/// when a tool starts, whatever was said before it is finished and will not be
/// in the final reply — so it is re-issued as `Said`, and the streaming text
/// starts again from nothing.
#[derive(Debug, Default)]
struct Activity {
    /// Finished lines and tool steps, in order.
    log: Vec<Step>,
    /// Reasoning since the last tool.
    reasoning: String,
    /// Text since the last tool.
    narration: String,
}

impl Activity {
    /// Record a step and return what the window should be told.
    fn observe(&mut self, step: Step) -> Vec<Step> {
        match step {
            Step::Reasoning { ref text } => {
                self.reasoning.push_str(text);
                vec![step]
            }
            Step::Text { ref text } => {
                self.narration.push_str(text);
                vec![step]
            }
            Step::ToolStarted { .. } => {
                let mut out = Vec::new();
                let said = self.narration.trim();
                if !said.is_empty() {
                    let said = Step::Said {
                        text: said.to_string(),
                    };
                    self.log.push(said.clone());
                    out.push(said);
                }
                self.narration.clear();
                self.reasoning.clear();
                self.log.push(step.clone());
                out.push(step);
                out
            }
            Step::ToolFinished { .. } => {
                self.log.push(step.clone());
                vec![step]
            }
            // Transient: by the time anyone replays, the tool has started or
            // the preparation was abandoned.
            Step::Preparing { .. } | Step::Said { .. } => vec![step],
        }
    }

    /// Everything shown so far, as the steps that would show it again.
    fn replay(&self) -> Vec<Step> {
        let mut steps = self.log.clone();
        if !self.reasoning.is_empty() {
            steps.push(Step::Reasoning {
                text: self.reasoning.clone(),
            });
        }
        if !self.narration.is_empty() {
            steps.push(Step::Text {
                text: self.narration.clone(),
            });
        }
        steps
    }
}

/// The answers the window will send, and nothing else.
///
/// **`always` is deliberately absent (FLOTTA-62).** Hermes accepts it, and
/// what it does is not what a button beside one command implies: it records
/// the command's *pattern* in `command_allowlist` in the agent's
/// `config.yaml`, on its volume, so one click on a card about a single
/// temp-directory delete permanently let that agent run any "delete in root
/// path" command without asking — surviving restarts, re-images and Hermes
/// updates, with no way to see or undo it from the app. A permanent grant
/// should be a deliberate act, not a button next to Deny.
///
/// Filtered twice on purpose: `read_approval` drops it from what the window is
/// shown, and `valid_choice` refuses to send it, so a crafted call from the
/// webview cannot reach the gateway with it either.
///
/// Also checked because `approval.respond` defaults an unrecognised choice to
/// `deny` — safe, but silent, so a typo would read as the person refusing.
pub const APPROVAL_CHOICES: [&str; 3] = ["once", "session", "deny"];

pub fn valid_choice(choice: &str) -> bool {
    APPROVAL_CHOICES.contains(&choice)
}

/// An `approval.request` payload, read field by field.
///
/// Tolerant for the same reason `read_resume` is: an approval the window
/// cannot fully parse must still be *shown*, because the alternative is the
/// original bug — an agent waiting on a prompt nobody sees. Missing `choices`
/// falls back to the two answers every gateway accepts.
fn read_approval(payload: &serde_json::Value) -> ApprovalRequest {
    let text = |key: &str| {
        payload
            .get(key)
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string()
    };
    let choices: Vec<String> = payload
        .get("choices")
        .and_then(|c| c.as_array())
        .map(|items| {
            items
                .iter()
                .filter_map(|c| c.as_str())
                .filter(|c| valid_choice(c))
                .map(str::to_string)
                .collect()
        })
        .filter(|c: &Vec<String>| !c.is_empty())
        .unwrap_or_else(|| vec!["once".into(), "deny".into()]);
    ApprovalRequest {
        request_id: payload
            .get("request_id")
            .and_then(|v| v.as_str())
            .filter(|id| !id.is_empty())
            .map(str::to_string),
        command: text("command"),
        description: text("description"),
        pattern: payload
            .get("pattern_key")
            .and_then(|v| v.as_str())
            .map(str::trim)
            .filter(|k| !k.is_empty())
            .map(str::to_string),
        choices,
    }
}

impl Conversations {
    /// The sender for a box, if there is a task still reading from it.
    ///
    /// `is_closed` is the honest test: it becomes true the moment the receiver
    /// drops, which is the moment the task returns. Checking only for the
    /// key's presence treats a finished conversation as a live one.
    pub fn live(&self, box_name: &str) -> Option<mpsc::Sender<Command>> {
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

    pub fn insert(&self, box_name: String, sender: mpsc::Sender<Command>) {
        self.0.lock().unwrap().insert(box_name, sender);
    }

    pub fn forget(&self, box_name: &str) {
        self.0.lock().unwrap().remove(box_name);
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
        Vec<HistoryLine>,
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

    let (session_id, resumed) = attach(&mut socket, settings, box_name).await?;
    Ok((socket, session_id, resumed))
}

/// Rejoin the conversation the box was already having, or start one.
///
/// M8.2 called `session.create` unconditionally, so switching agents and back
/// showed a blank transcript against an agent that remembered the whole thing
/// — the app contradicting the product's central claim.
///
/// The transcript is *asked for*, never cached. The agent's memory is the
/// source of truth; a client holding its own copy would keep showing it after
/// the agent had moved on.
async fn attach(
    socket: &mut Socket,
    settings: &Settings,
    box_name: &str,
) -> Result<(String, Vec<HistoryLine>), FleetError> {
    // **Read now, not once at connect.** The first version captured this when
    // the conversation task started and reused it for every resync, so an
    // agent whose instructions changed *during* the conversation could never
    // notice: saving from the Info panel unmounts the transcript, and coming
    // back runs a resync that compared against a timestamp from before the
    // edit. It resumed the old session and the agent kept the old persona,
    // with nothing on screen to say why.
    //
    // Best-effort by construction. A metadata read that fails must not make an
    // agent unreachable — the cost of missing it is a conversation that needed
    // restarting and did not, which is the state we are already in.
    let instructions_at = crate::fleet::get_box(settings, box_name)
        .await
        .ok()
        .and_then(|row| row.instructions_changed_at);
    // `session.most_recent` skips tool-source sessions, so it returns the
    // conversational one rather than whatever a background task last touched.
    let recent = call(socket, "session.most_recent", serde_json::json!({})).await?;
    let existing = recent
        .get("session_id")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty());

    // Instructions changed since this conversation began, so resuming it would
    // put the agent back in the persona it was told to stop having. A session
    // renders its system prompt once and keeps it, so the only way the new
    // SOUL.md is read is a new session.
    if stale_identity(
        instructions_at,
        recent.get("started_at").and_then(|v| v.as_f64()),
    ) {
        return Ok((create_session(socket).await?, Vec::new()));
    }

    if let Some(session_id) = existing {
        let resumed = call(
            socket,
            "session.resume",
            serde_json::json!({"session_id": session_id}),
        )
        .await?;
        return Ok(read_resume(&resumed, session_id));
    }

    Ok((create_session(socket).await?, Vec::new()))
}

/// Whether the conversation we were about to resume predates the agent's
/// current instructions.
///
/// Both arguments are seconds since the Unix epoch: `changed` from the control
/// plane, `started` from the box's own `session.most_recent`. They come from
/// two different clocks, which is why this is a plain comparison with no
/// tolerance window — a fudge factor would be guessing at skew we cannot
/// measure, and being wrong in the safe direction (a needless fresh
/// conversation) costs a transcript nobody asked to keep, while being wrong
/// the other way is the bug this whole ticket is about.
///
/// Missing either side means **resume**. `changed` is `None` for every agent
/// whose instructions have never been rewritten, which is most of them;
/// `started` is `None` when there is no session to resume, and creating one is
/// what `attach` does next anyway.
fn stale_identity(changed: Option<f64>, started: Option<f64>) -> bool {
    match (changed, started) {
        (Some(changed), Some(started)) => changed > started,
        _ => false,
    }
}

/// Open a brand-new conversation on a socket that is already connected.
///
/// The id `session.create` answers with is the live one — the id
/// `prompt.submit` takes. That is *not* true of `session.resume`, whose reply
/// names a different id from the one it was asked for, which is why these two
/// paths are written out separately rather than shared.
async fn create_session(socket: &mut Socket) -> Result<String, FleetError> {
    let created = call(socket, "session.create", serde_json::json!({})).await?;
    read_created(&created)
        .ok_or_else(|| FleetError::Unexpected("session.create returned no id".into()))
}

/// The session id a `session.create` reply names, if it named a usable one.
///
/// An **empty** id is refused rather than carried. Taken as the session, it
/// would be sent with every `prompt.submit` and come back as `session not
/// found` — a failure that reads as the box having lost the conversation,
/// several turns away from the reply that was actually malformed.
fn read_created(payload: &serde_json::Value) -> Option<String> {
    payload
        .get("session_id")
        .and_then(|id| id.as_str())
        .filter(|id| !id.is_empty())
        .map(str::to_string)
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

/// A resumed conversation, as lines a transcript can show.
///
/// Read field by field rather than deserialised into a struct, and that is the
/// point. A Hermes message carries `row_id`, `timestamp`, `reasoning` and more,
/// and not every one has `text` — a tool call may have none at all. Decoding
/// into `Vec<HistoryLine>` therefore failed on the whole array, and the
/// `.ok().unwrap_or_default()` around it turned that failure into "this agent
/// remembers nothing".
///
/// Which is the same mistake as "No agents yet", one layer down: a parse
/// failure rendered as an empty truth. So this cannot fail — it takes what it
/// understands and drops what it does not, because one unreadable line is not
/// a reason to forget the conversation around it.
fn history_from(messages: Option<&serde_json::Value>) -> Vec<HistoryLine> {
    let Some(items) = messages.and_then(|m| m.as_array()) else {
        return Vec::new();
    };
    items
        .iter()
        .filter_map(|item| {
            let text = item
                .get("text")
                .and_then(|t| t.as_str())
                .unwrap_or("")
                .trim();
            if text.is_empty() {
                // A tool call with no display text adds nothing to a
                // transcript and would render as an empty bubble.
                return None;
            }
            Some(HistoryLine {
                role: item
                    .get("role")
                    .and_then(|r| r.as_str())
                    .unwrap_or("assistant")
                    .to_string(),
                text: text.to_string(),
            })
        })
        .collect()
}

/// The session to talk to, and the transcript, from a `session.resume` reply.
///
/// **The id a resume answers with is not the one it was asked for.** The
/// gateway follows a compression tip to the live session and echoes the
/// request back as `resumed`, while `session_id` names the session that now
/// exists in memory. Submitting against the requested id returns
/// `{"code":4001,"message":"session not found"}` — from a session that has
/// just resumed successfully.
///
/// This exists because that was learned once, written down in `attach`, and
/// then reintroduced forty lines later in the resync path, where the same
/// stale id would have killed the conversation on the *next* message rather
/// than immediately. One function, two callers, no second chance to forget.
fn read_resume(payload: &serde_json::Value, requested: &str) -> (String, Vec<HistoryLine>) {
    let live = payload
        .get("session_id")
        .and_then(|s| s.as_str())
        .unwrap_or(requested);
    (live.to_string(), history_from(payload.get("messages")))
}

/// One request, and the `result` that answers it.
///
/// Generalised from `create_session` because reattachment needs three of these
/// and they differ only in the method name — and because every one of them has
/// to survive the same thing: events interleave with responses constantly, so
/// a reply is found by id, not by being next.
async fn call(
    socket: &mut Socket,
    method: &str,
    params: serde_json::Value,
) -> Result<serde_json::Value, FleetError> {
    let id = rpc(socket, method, params).await?;
    // A wall-clock deadline, not a per-frame one: events interleave with
    // responses, and resetting the clock on every ignored event means a chatty
    // gateway that never answers keeps the handshake alive forever.
    let deadline = tokio::time::Instant::now() + WAKE_TIMEOUT;
    loop {
        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        if remaining.is_zero() {
            return Err(FleetError::Unreachable(format!(
                "the agent did not answer {method}"
            )));
        }
        let frame = next_frame(socket, remaining).await?;
        if frame.get("id").and_then(|v| v.as_u64()) != Some(id) {
            continue;
        }
        if let Some(error) = frame.get("error") {
            return Err(FleetError::Unexpected(format!(
                "the agent refused {method}: {error}"
            )));
        }
        return Ok(frame.get("result").cloned().unwrap_or_default());
    }
}

/// Submit one prompt and wait for the agent's answer.
/// Something a turn needs the window to know while it is still running.
enum TurnNote<'a> {
    /// Hermes stopped to ask permission.
    Approval(&'a ApprovalRequest),
    /// The agent did something the window can show.
    Progress(Step),
    /// The transcript pane remounted mid-turn and needs telling again that
    /// work is in flight — otherwise it sits on "waking" until the reply.
    StillThinking,
}

/// One prompt, from submit to reply.
///
/// **It listens for the person as well as the agent.** The first version
/// awaited socket frames and nothing else, so a turn that tripped Hermes's
/// approval gate could not have been answered even if the window had shown
/// the prompt: the click would have queued behind the very turn it was meant
/// to unblock. So this selects over three things — the agent's frames, the
/// window's commands, and the keep-alive — for the whole turn.
///
/// The keep-alive matters more here than it did. A turn used to be a model
/// call with frames flowing; a turn waiting on a person is silence, and
/// Cloudflare closes an idle proxied socket at around a hundred seconds
/// without telling either end.
///
/// Commands that are not answers — a resync, a reset, another prompt — are
/// **deferred** rather than dropped, and run in order once the turn ends.
async fn one_turn(
    socket: &mut Socket,
    session_id: &str,
    text: &str,
    commands: &mut mpsc::Receiver<Command>,
    deferred: &mut VecDeque<Command>,
    ping: &mut tokio::time::Interval,
    mut note: impl FnMut(TurnNote),
) -> Result<String, FleetError> {
    let id = rpc(
        socket,
        "prompt.submit",
        serde_json::json!({"session_id": session_id, "text": text}),
    )
    .await?;

    let deadline = tokio::time::Instant::now() + TURN_TIMEOUT;
    let mut pending: Option<ApprovalRequest> = None;
    let mut activity = Activity::default();
    loop {
        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        if remaining.is_zero() {
            return Err(FleetError::Unreachable(
                "the agent did not answer in time".into(),
            ));
        }

        tokio::select! {
            frame = next_frame(socket, remaining) => {
                match interpret(&frame?, id) {
                    Verdict::Ignore => {}
                    Verdict::Approval(request) => {
                        note(TurnNote::Approval(&request));
                        pending = Some(request);
                    }
                    Verdict::Progress(step) => {
                        for step in activity.observe(step) {
                            note(TurnNote::Progress(step));
                        }
                    }
                    Verdict::Reply(text) => return Ok(text),
                    Verdict::Failed(err) => return Err(err),
                }
            }
            command = commands.recv() => match command {
                Some(Command::Approve { request_id, choice }) => {
                    // The window's id wins; the pending one is the fallback for
                    // a click that arrived without one. With neither, the
                    // gateway resolves the oldest approval in the session.
                    let request_id = request_id
                        .or_else(|| pending.as_ref().and_then(|p| p.request_id.clone()));
                    let mut params = serde_json::json!({
                        "session_id": session_id,
                        "choice": choice,
                    });
                    if let Some(request_id) = request_id {
                        params["request_id"] = serde_json::json!(request_id);
                    }
                    rpc(socket, "approval.respond", params).await?;
                    pending = None;
                }
                Some(Command::Resync) => {
                    // Switching away and back mid-turn remounts the pane with
                    // nothing in it. Say work is in flight, and put a pending
                    // question back in front of the person — an approval that
                    // vanished on a tab switch is the original bug again.
                    note(TurnNote::StillThinking);
                    // And what it has done so far. A remount clears the pane,
                    // and a turn that had run four commands would otherwise
                    // come back reading as one that had only just begun.
                    for step in activity.replay() {
                        note(TurnNote::Progress(step));
                    }
                    if let Some(request) = &pending {
                        note(TurnNote::Approval(request));
                    }
                    deferred.push_back(Command::Resync);
                }
                Some(other) => deferred.push_back(other),
                None => {
                    return Err(FleetError::Unreachable(
                        "the conversation was closed".into(),
                    ))
                }
            },
            _ = ping.tick() => {
                socket
                    .send(Message::Ping(Vec::new()))
                    .await
                    .map_err(|_| FleetError::Unreachable("the connection to the agent dropped".into()))?;
            }
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
    /// The agent stopped to ask. The turn is still in flight.
    Approval(ApprovalRequest),
    /// The agent did something. The turn is still in flight.
    Progress(Step),
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
    match params.get("type").and_then(|t| t.as_str()) {
        Some("message.complete") => {}
        Some("approval.request") => {
            let payload = params.get("payload").cloned().unwrap_or_default();
            return Verdict::Approval(read_approval(&payload));
        }
        Some(kind) => {
            let payload = params.get("payload").cloned().unwrap_or_default();
            return match read_step(kind, &payload) {
                Some(step) => Verdict::Progress(step),
                None => Verdict::Ignore,
            };
        }
        None => return Verdict::Ignore,
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
    mut commands: mpsc::Receiver<Command>,
) {
    AgentEvent::Waking {
        box_name: box_name.clone(),
    }
    .emit(&app);

    let (mut socket, mut session_id, resumed) = match connect(&settings, &box_name, &token).await {
        Ok(pair) => pair,
        Err(err) => {
            finish(&app, &box_name, Some(err.detail().to_string()));
            return;
        }
    };

    AgentEvent::Ready {
        box_name: box_name.clone(),
        resumed,
    }
    .emit(&app);

    let mut ping = tokio::time::interval(PING_EVERY);
    ping.tick().await; // the first tick is immediate

    // Commands that arrived during a turn, in the order they arrived.
    let mut deferred: VecDeque<Command> = VecDeque::new();

    loop {
        let command = if let Some(command) = deferred.pop_front() {
            command
        } else {
            tokio::select! {
                command = commands.recv() => {
                    let Some(command) = command else {
                        finish(&app, &box_name, None);
                        return;
                    };
                    command
                }
                _ = ping.tick() => {
                    // Cloudflare closes an idle proxied socket without telling
                    // either end, and the symptom is a reply that never arrives.
                    if socket.send(Message::Ping(Vec::new())).await.is_err() {
                        finish(&app, &box_name, Some("the connection to the agent dropped".into()));
                        return;
                    }
                    continue;
                }
            }
        };

        match command {
            Command::Resync => {
                // The same read `attach` does at connect, deliberately.
                //
                // The first version resumed `session_id` directly and failed
                // against a real box with `{"code":4007,"message":"session not
                // found"}` — because **there are two ids**. `session.resume`
                // looks its argument up in the *database*; the id it hands back
                // is the live in-memory key that `prompt.submit` needs.
                // Resuming the live one asks the database for a row that was
                // never in it. Re-running `attach` keeps exactly one path that
                // knows which id is which.
                match attach(&mut socket, &settings, &box_name).await {
                    Ok((live, resumed)) => {
                        session_id = live;
                        AgentEvent::Ready {
                            box_name: box_name.clone(),
                            resumed,
                        }
                        .emit(&app);
                    }
                    Err(err) => {
                        finish(&app, &box_name, Some(err.detail().to_string()));
                        return;
                    }
                }
            }
            Command::Reset => match create_session(&mut socket).await {
                Ok(fresh) => {
                    session_id = fresh;
                    AgentEvent::Reset {
                        box_name: box_name.clone(),
                    }
                    .emit(&app);
                }
                Err(err) => {
                    finish(&app, &box_name, Some(err.detail().to_string()));
                    return;
                }
            },
            Command::Prompt(prompt) => {
                AgentEvent::Thinking {
                    box_name: box_name.clone(),
                }
                .emit(&app);
                let result = one_turn(
                    &mut socket,
                    &session_id,
                    &prompt,
                    &mut commands,
                    &mut deferred,
                    &mut ping,
                    |note| match note {
                        TurnNote::Approval(request) => AgentEvent::Approval {
                            box_name: box_name.clone(),
                            request: request.clone(),
                        }
                        .emit(&app),
                        TurnNote::StillThinking => AgentEvent::Thinking {
                            box_name: box_name.clone(),
                        }
                        .emit(&app),
                        TurnNote::Progress(step) => AgentEvent::Progress {
                            box_name: box_name.clone(),
                            step,
                        }
                        .emit(&app),
                    },
                )
                .await;
                match result {
                    Ok(text) => AgentEvent::Reply {
                        box_name: box_name.clone(),
                        text,
                    }
                    .emit(&app),
                    Err(err) => {
                        finish(&app, &box_name, Some(err.detail().to_string()));
                        return;
                    }
                }
            }
            // Between turns nothing is waiting on an answer: the approval it
            // meant has already been decided, by the gate's timeout if nothing
            // else. Dropped rather than sent, so a late click cannot resolve
            // an approval that belongs to a later turn.
            Command::Approve { .. } => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// One turn with nobody at the window: no commands will arrive and no
    /// approval will be answered. The live tests are about the protocol, and
    /// giving them a real command channel would test the window instead.
    async fn turn_for_test(
        socket: &mut Socket,
        session_id: &str,
        text: &str,
    ) -> Result<String, FleetError> {
        let (_tx, mut rx) = mpsc::channel(1);
        let mut deferred = VecDeque::new();
        let mut ping = tokio::time::interval(PING_EVERY);
        ping.tick().await;
        one_turn(
            socket,
            session_id,
            text,
            &mut rx,
            &mut deferred,
            &mut ping,
            |_| {},
        )
        .await
    }

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
        // `thinking.delta` is Hermes's terminal spinner, not the agent: a
        // window that showed it would print "◉_◉ processing..." mid-transcript.
        for kind in [
            "gateway.ready",
            "session.updated",
            "thinking.delta",
            "session.usage",
        ] {
            let v = interpret(&serde_json::json!({"params": {"type": kind}}), 7);
            assert!(matches!(v, Verdict::Ignore), "{kind} should be ignored");
        }
    }

    /// A frame as the gateway sends it, for a progress event.
    fn event(kind: &str, payload: serde_json::Value) -> serde_json::Value {
        serde_json::json!({
            "jsonrpc": "2.0",
            "method": "event",
            "params": {"type": kind, "seq": 19, "session_id": "4c0c1e22", "payload": payload},
        })
    }

    fn step_of(frame: serde_json::Value) -> Step {
        match interpret(&frame, 7) {
            Verdict::Progress(step) => step,
            other => panic!("expected a step, got {other:?}"),
        }
    }

    #[test]
    fn a_tool_starting_is_shown_with_what_it_was_asked_to_do() {
        // Captured from eng-g, v2026.9.11.
        let step = step_of(event(
            "tool.start",
            serde_json::json!({"args": {"command": "ls /workspace"}, "context": "ls /workspace",
                               "name": "terminal", "tool_id": "call_a58e614a5dbe4cceb84a0125"}),
        ));
        assert_eq!(
            step,
            Step::ToolStarted {
                id: "call_a58e614a5dbe4cceb84a0125".into(),
                tool: "terminal".into(),
                detail: "ls /workspace".into(),
            }
        );
    }

    #[test]
    fn a_tool_finishing_carries_its_duration_and_not_its_output() {
        let step = step_of(event(
            "tool.complete",
            serde_json::json!({"args": {"command": "uname -a"}, "duration_s": 0.1347668170928955,
                               "name": "terminal",
                               "result": {"error": null, "exit_code": 0, "output": "Linux 815990c9246728 6.12.105-fly"},
                               "tool_id": "call_3edca261bef64a5895988675"}),
        ));
        assert_eq!(
            step,
            Step::ToolFinished {
                id: "call_3edca261bef64a5895988675".into(),
                tool: "terminal".into(),
                seconds: Some(0.1347668170928955),
                failed: false,
            }
        );
        // The output can be megabytes; the window is told the step, not the data.
        assert!(!serde_json::to_string(&step).unwrap().contains("Linux"));
    }

    #[test]
    fn a_command_that_ran_and_failed_is_shown_as_failed() {
        // eng-r's slow turn: three commands failed and every one looked like
        // progress. A non-zero exit is a failure even with no `error`.
        let exited = step_of(event(
            "tool.complete",
            serde_json::json!({"tool_id": "t", "name": "terminal",
                               "result": {"error": null, "exit_code": 2, "output": "usage: flotta repo"}}),
        ));
        assert!(matches!(exited, Step::ToolFinished { failed: true, .. }));

        let broke = step_of(event(
            "tool.complete",
            serde_json::json!({"tool_id": "t", "name": "read_file",
                               "result": {"error": "no such file"}}),
        ));
        assert!(matches!(broke, Step::ToolFinished { failed: true, .. }));
    }

    #[test]
    fn a_tool_whose_result_is_not_an_object_still_finishes() {
        // Not every tool returns the terminal's shape. A result the window
        // cannot read must not leave the step spinning forever.
        let step = step_of(event(
            "tool.complete",
            serde_json::json!({"tool_id": "t", "name": "web_search", "result": "three hits"}),
        ));
        assert!(matches!(
            step,
            Step::ToolFinished {
                failed: false,
                seconds: None,
                ..
            }
        ));
    }

    #[test]
    fn a_long_or_multi_line_context_becomes_one_bounded_line() {
        let long = format!("cat <<'EOF' > notes.md\n{}\nEOF", "word ".repeat(200));
        let Step::ToolStarted { detail, .. } = step_of(event(
            "tool.start",
            serde_json::json!({"tool_id": "t", "name": "terminal", "context": long}),
        )) else {
            panic!("not a tool start");
        };
        assert!(!detail.contains('\n'), "{detail}");
        assert_eq!(detail.chars().count(), DETAIL_CHARS);
        assert!(detail.ends_with('…'));
    }

    #[test]
    fn streaming_text_and_reasoning_are_steps_and_empty_pieces_are_not() {
        assert_eq!(
            step_of(event(
                "message.delta",
                serde_json::json!({"text": "` is empty,"})
            )),
            Step::Text {
                text: "` is empty,".into()
            }
        );
        assert_eq!(
            step_of(event(
                "reasoning.delta",
                serde_json::json!({"text": " run two"})
            )),
            Step::Reasoning {
                text: " run two".into()
            }
        );
        // The gateway sends empty deltas between phases.
        for kind in ["message.delta", "reasoning.delta"] {
            let v = interpret(&event(kind, serde_json::json!({"text": ""})), 7);
            assert!(matches!(v, Verdict::Ignore), "{kind} with no text");
        }
    }

    #[test]
    fn preparing_a_tool_names_it() {
        assert_eq!(
            step_of(event(
                "tool.generating",
                serde_json::json!({"name": "terminal"})
            )),
            Step::Preparing {
                tool: "terminal".into()
            }
        );
    }

    fn started(id: &str) -> Step {
        Step::ToolStarted {
            id: id.into(),
            tool: "terminal".into(),
            detail: "ls".into(),
        }
    }

    fn text(t: &str) -> Step {
        Step::Text { text: t.into() }
    }

    #[test]
    fn narration_before_a_tool_becomes_a_line_of_its_own() {
        // The final reply does not repeat it, so left as streaming text it
        // would be replaced by the reply and vanish.
        let mut activity = Activity::default();
        activity.observe(text("Let me "));
        activity.observe(text("check.\n"));
        let out = activity.observe(started("a"));
        assert_eq!(
            out,
            vec![
                Step::Said {
                    text: "Let me check.".into()
                },
                started("a")
            ]
        );
        // And the streaming text starts again from nothing.
        assert!(activity.narration.is_empty());
    }

    #[test]
    fn a_tool_with_nothing_said_before_it_adds_no_empty_line() {
        let mut activity = Activity::default();
        activity.observe(text("\n\n"));
        assert_eq!(activity.observe(started("a")), vec![started("a")]);
    }

    #[test]
    fn a_remounted_pane_is_shown_the_turn_so_far() {
        let mut activity = Activity::default();
        activity.observe(Step::Reasoning {
            text: "old thought".into(),
        });
        activity.observe(started("a"));
        let finished = Step::ToolFinished {
            id: "a".into(),
            tool: "terminal".into(),
            seconds: Some(0.2),
            failed: false,
        };
        activity.observe(finished.clone());
        activity.observe(Step::Reasoning {
            text: "new ".into(),
        });
        activity.observe(Step::Reasoning {
            text: "thought".into(),
        });
        activity.observe(Step::Preparing {
            tool: "terminal".into(),
        });
        activity.observe(text("It is "));
        activity.observe(text("empty"));

        assert_eq!(
            activity.replay(),
            vec![
                started("a"),
                finished,
                // Only reasoning since the last tool: the old thought led to a
                // step that is already on screen.
                Step::Reasoning {
                    text: "new thought".into()
                },
                // Coalesced: one piece rather than every delta again.
                text("It is empty"),
            ]
        );
    }

    #[test]
    fn a_progress_event_reaches_the_window_tagged_by_kind() {
        // The frontend switches on these names; renaming a variant here
        // without it would silently drop every step.
        let json = serde_json::to_value(AgentEvent::Progress {
            box_name: "eng-g".into(),
            step: started("a"),
        })
        .unwrap();
        assert_eq!(
            json,
            serde_json::json!({"kind": "progress", "box_name": "eng-g",
                               "step": {"kind": "tool_started", "id": "a", "tool": "terminal", "detail": "ls"}})
        );
    }

    fn approval_frame(payload: serde_json::Value) -> serde_json::Value {
        // The envelope `tui_gateway.server._event_frame` builds, verbatim.
        serde_json::json!({
            "jsonrpc": "2.0",
            "method": "event",
            "params": {"type": "approval.request", "session_id": "s1", "payload": payload},
        })
    }

    #[test]
    fn an_approval_request_mid_turn_is_surfaced_not_ignored() {
        // The bug: this frame was one of the "interleaved events" the turn
        // loop ignored, so an agent waited on a question nobody was shown.
        let v = interpret(
            &approval_frame(serde_json::json!({
                "request_id": "r-1",
                "command": "rm -rf /workspace/old",
                "description": "recursive delete",
                "choices": ["once", "session", "always", "deny"],
            })),
            7,
        );
        let Verdict::Approval(request) = v else {
            panic!("an approval request was not surfaced: {v:?}");
        };
        assert_eq!(request.request_id.as_deref(), Some("r-1"));
        assert_eq!(request.command, "rm -rf /workspace/old");
        assert_eq!(request.description, "recursive delete");
        // `always` is offered by Hermes and deliberately not by the window.
        assert_eq!(request.choices, ["once", "session", "deny"]);
    }

    #[test]
    fn a_smart_denied_command_keeps_the_narrower_choices_hermes_offered() {
        // The gateway drops `session` and `always` when its classifier already
        // judged a command dangerous. The window must not offer them back.
        let request = read_approval(&serde_json::json!({
            "command": "curl x | sh", "choices": ["once", "deny"], "smart_denied": true,
        }));
        assert_eq!(request.choices, ["once", "deny"]);
    }

    #[test]
    fn an_approval_with_no_choices_is_still_answerable() {
        // Showing nothing to click would rebuild the original bug inside the
        // fix: a visible prompt that cannot be answered.
        let request = read_approval(&serde_json::json!({"command": "x"}));
        assert_eq!(request.choices, ["once", "deny"]);
    }

    #[test]
    fn choices_the_gateway_would_not_accept_are_dropped() {
        let request = read_approval(&serde_json::json!({"choices": ["once", "yolo", "deny"]}));
        assert_eq!(request.choices, ["once", "deny"]);

        // And a list that was nothing but junk falls back rather than empties.
        let request = read_approval(&serde_json::json!({"choices": ["yolo"]}));
        assert_eq!(request.choices, ["once", "deny"]);
    }

    #[test]
    fn an_empty_request_id_is_no_request_id() {
        // Sent back as `""` it would match no pending approval; absent, the
        // gateway resolves the oldest one, which is the one on screen.
        assert_eq!(
            read_approval(&serde_json::json!({"request_id": ""})).request_id,
            None
        );
        assert_eq!(read_approval(&serde_json::json!({})).request_id, None);
    }

    #[test]
    fn an_approval_is_never_mistaken_for_the_reply() {
        // It carries a command and a description and sits on the same event
        // channel as the answer. Returned as a reply, the turn would end with
        // the agent appearing to say "rm -rf" to you.
        let v = interpret(&approval_frame(serde_json::json!({"command": "x"})), 7);
        assert!(!matches!(v, Verdict::Reply(_)));
    }

    #[test]
    fn always_is_never_offered_even_when_hermes_offers_it() {
        // FLOTTA-62. Hermes records `always` by *pattern*, permanently, on the
        // agent's volume — so one click beside one command allowed a whole
        // category of commands forever, invisibly. The window shows it no more.
        let request = read_approval(&serde_json::json!({
            "command": "rm -rf /tmp/x",
            "pattern_key": "delete in root path",
            "choices": ["once", "session", "always", "deny"],
        }));
        assert!(!request.choices.iter().any(|c| c == "always"));
    }

    #[test]
    fn always_cannot_be_sent_even_by_a_crafted_call() {
        // Dropping it from the card is not enough on its own: the webview can
        // invoke `respond_approval` with any string it likes.
        assert!(!valid_choice("always"));
    }

    #[test]
    fn a_card_that_offered_only_always_still_has_something_to_click() {
        // Filtering must not leave an unanswerable prompt — the original bug.
        let request = read_approval(&serde_json::json!({"choices": ["always"]}));
        assert_eq!(request.choices, ["once", "deny"]);
    }

    #[test]
    fn the_pattern_a_grant_would_cover_is_carried_to_the_window() {
        let request = read_approval(&serde_json::json!({"pattern_key": " delete in root path "}));
        assert_eq!(request.pattern.as_deref(), Some("delete in root path"));
        assert_eq!(
            read_approval(&serde_json::json!({"pattern_key": ""})).pattern,
            None
        );
        assert_eq!(read_approval(&serde_json::json!({})).pattern, None);
    }

    #[test]
    fn only_the_answers_the_window_offers_are_valid() {
        for choice in ["once", "session", "deny"] {
            assert!(valid_choice(choice), "{choice} was refused");
        }
        // `approval.respond` defaults an unknown choice to deny — safe, but
        // silent. These must be refused in the app instead.
        for choice in ["", "yes", "approve", "Deny", "allow"] {
            assert!(!valid_choice(choice), "{choice:?} was accepted");
        }
    }

    #[test]
    fn history_survives_fields_it_does_not_model() {
        // The exact shape a live box returned. `row_id`, `timestamp` and
        // `reasoning` are not modelled here, and decoding the array into a
        // struct failed on them — which, wrapped in `.ok().unwrap_or_default()`,
        // read as "this agent remembers nothing" about a box holding fifteen
        // turns of real conversation.
        let messages = serde_json::json!([
            {"role": "user", "row_id": 130, "text": "hey", "timestamp": 1788470150.79},
            {"role": "assistant", "row_id": 131, "text": "Hey! What can I help with?",
             "reasoning": "The user is saying hey.",
             "reasoning_content": "The user is saying hey."}
        ]);
        let lines = history_from(Some(&messages));
        assert_eq!(lines.len(), 2);
        assert_eq!(lines[0].role, "user");
        assert_eq!(lines[0].text, "hey");
    }

    #[test]
    fn one_unreadable_line_does_not_lose_the_conversation() {
        // A tool call carries no display text. Dropping the array over it
        // would forget everything said around it.
        let messages = serde_json::json!([
            {"role": "user", "text": "run the tests"},
            {"role": "assistant", "tool_calls": [{"name": "shell"}]},
            {"role": "assistant", "text": "all 622 passed"}
        ]);
        let lines = history_from(Some(&messages));
        assert_eq!(lines.len(), 2, "the tool call is skipped, not fatal");
        assert_eq!(lines[1].text, "all 622 passed");
    }

    #[test]
    fn an_empty_message_is_not_a_blank_bubble() {
        let messages = serde_json::json!([{"role": "assistant", "text": "   "}]);
        assert!(history_from(Some(&messages)).is_empty());
    }

    #[test]
    fn a_new_session_has_no_history_and_that_is_not_an_error() {
        assert!(history_from(None).is_empty());
        assert!(history_from(Some(&serde_json::Value::Null)).is_empty());
    }

    #[test]
    fn a_resume_is_answered_by_the_live_session_not_the_one_asked_for() {
        // The gateway follows a compression tip and echoes the request back as
        // `resumed`. Submitting against the requested id returns 4001 from a
        // session that just resumed successfully — learned once at connect,
        // then reintroduced in the resync path where it would have killed the
        // conversation on the *next* message.
        let payload = serde_json::json!({
            "resumed": "asked-for",
            "session_id": "the-live-one",
            "messages": [{"role": "user", "text": "hey"}]
        });
        let (live, history) = read_resume(&payload, "asked-for");
        assert_eq!(live, "the-live-one");
        assert_eq!(history.len(), 1);
    }

    #[test]
    fn a_resume_with_no_id_falls_back_to_the_one_asked_for() {
        let payload = serde_json::json!({"messages": []});
        let (live, history) = read_resume(&payload, "asked-for");
        assert_eq!(live, "asked-for");
        assert!(history.is_empty());
    }

    #[test]
    fn a_resumed_session_with_no_messages_is_not_an_error() {
        // A box that has been talked to but has nothing displayable — every
        // turn a tool call, say. Empty history, still a usable session.
        let payload = serde_json::json!({"session_id": "s1"});
        let (live, history) = read_resume(&payload, "asked-for");
        assert_eq!(live, "s1");
        assert!(history.is_empty());
    }

    #[test]
    fn instructions_newer_than_the_conversation_mean_a_fresh_one() {
        // The whole ticket in one line: the session was born before the
        // persona it is supposed to have, so resuming it would keep the old
        // one forever.
        assert!(stale_identity(Some(200.0), Some(100.0)));
    }

    #[test]
    fn a_conversation_started_after_the_instructions_is_resumed() {
        assert!(!stale_identity(Some(100.0), Some(200.0)));
    }

    #[test]
    fn instructions_that_never_changed_never_throw_away_a_transcript() {
        // `None` is most of the fleet: an agent seeded at creation and never
        // edited. Treating an absent timestamp as "infinitely old" would start
        // a fresh conversation with every agent on every open.
        assert!(!stale_identity(None, Some(100.0)));
    }

    #[test]
    fn no_conversation_to_resume_is_not_a_stale_one() {
        // `attach` creates one immediately after, so answering true here would
        // be a second create racing the first.
        assert!(!stale_identity(Some(200.0), None));
        assert!(!stale_identity(None, None));
    }

    #[test]
    fn a_dead_heat_resumes() {
        // Equal is not newer. Two clocks that agree to the microsecond is
        // vanishingly unlikely, and the tie-break that costs a transcript is
        // the wrong one to pick for free.
        assert!(!stale_identity(Some(100.0), Some(100.0)));
    }

    #[test]
    fn a_created_session_names_the_id_prompts_are_sent_against() {
        // Unlike `session.resume`, `session.create` answers with the live id
        // directly — there is no second id to keep in step.
        let payload = serde_json::json!({"session_id": "fresh", "info": {}});
        assert_eq!(read_created(&payload).as_deref(), Some("fresh"));
    }

    #[test]
    fn a_create_reply_without_an_id_is_not_a_session() {
        assert_eq!(read_created(&serde_json::json!({})), None);
        assert_eq!(read_created(&serde_json::json!({"session_id": null})), None);
    }

    #[test]
    fn an_empty_created_id_is_refused_rather_than_carried() {
        // The one that matters. Carried, an empty id becomes the session every
        // `prompt.submit` names, and the box answers `session not found` — a
        // failure that reads as the conversation having been lost, several
        // turns away from the malformed reply that caused it.
        assert_eq!(read_created(&serde_json::json!({"session_id": ""})), None);
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
        // **The real control URL, not an empty string.** `attach` asks the
        // control plane when this agent's instructions last changed, and with
        // no URL that read fails, falls back to `None`, and the staleness
        // check can never fire — so the test would sail past the one branch
        // FLOTTA-58 exists for while looking like it covered the path.
        let settings = Settings {
            control_url: std::env::var("FLOTTA_CONTROL_URL").unwrap_or_default(),
            domain: std::env::var("FLOTTA_DOMAIN").unwrap_or_else(|_| "flotta.dev".into()),
        };
        let box_name = std::env::var("FLOTTA_BOX").unwrap_or_else(|_| "eng-a".into());

        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();

        runtime.block_on(async {
            // First connection: say something distinctive.
            let (mut socket, session, resumed) = connect(&settings, &box_name, &token)
                .await
                .unwrap_or_else(|e| panic!("connect failed: {}", e.detail()));
            println!(
                "connected, session {session}, {} earlier turns",
                resumed.len()
            );

            // Unique per run: a box accumulates conversations, and yesterday's
            // would let a broken resume pass.
            let marker = format!("banana{}", std::process::id());
            let reply = turn_for_test(
                &mut socket,
                &session,
                &format!("Reply with exactly one word: {marker}. Nothing else."),
            )
            .await
            .unwrap_or_else(|e| panic!("turn failed: {}", e.detail()));
            println!("reply: {reply:?}");
            assert!(!reply.trim().is_empty(), "the agent replied with nothing");
            drop(socket);

            // Second connection: the transcript must come back **from the box**.
            // This is the acceptance criterion for reattachment, and the only
            // honest way to test it is to reconnect.
            let (_socket, resumed_session, history) = connect(&settings, &box_name, &token)
                .await
                .unwrap_or_else(|e| panic!("reconnect failed: {}", e.detail()));
            println!(
                "reconnected, session {resumed_session}, {} earlier turns",
                history.len()
            );
            for line in &history {
                println!(
                    "  {}: {}",
                    line.role,
                    line.text.chars().take(60).collect::<String>()
                );
            }

            assert!(
                history.iter().any(|line| line.text.contains(&marker)),
                "reconnecting did not bring the conversation back: {} lines, none with {marker}",
                history.len()
            );

            // **Resync on a socket that is already open.** This is what
            // switching back to an agent does, and it was the one assumption
            // in that fix with nothing behind it: `session.resume` is
            // documented for *reattaching*, and calling it again on a live
            // session could plausibly have returned nothing, or disturbed it.
            let mut socket = _socket;

            // Exactly what `Command::Resync` runs when you switch back to an
            // agent whose socket is still open.
            let (live, again) = attach(&mut socket, &settings, &box_name)
                .await
                .unwrap_or_else(|e| panic!("resync failed: {}", e.detail()));
            println!("resync -> session {live}, {} turns", again.len());
            assert!(
                again.iter().any(|line| line.text.contains(&marker)),
                "a resync on a live socket lost the conversation: {} lines",
                again.len()
            );

            // And the session stays usable afterwards — the failure the
            // reviewer caught lands here, one message after the resync.
            let after = turn_for_test(&mut socket, &live, "Reply with one word: still.")
                .await
                .unwrap_or_else(|e| panic!("the turn after a resync failed: {}", e.detail()));
            println!("after resync: {after:?}");
        });
    }

    /// **FLOTTA-60's acceptance, through the real turn loop.**
    ///
    /// Drives `one_turn` itself — the select over frames, commands and the
    /// keep-alive — against a real box, and answers the approval the way the
    /// window does: by sending `Command::Approve` down the conversation's own
    /// channel. A Python probe had already proven the protocol; what only this
    /// can prove is that *this* loop surfaces the question and carries the
    /// answer back while the turn is still in flight.
    ///
    /// The command removes a temp path that does not exist, which Hermes flags
    /// as "delete in root path" and escalates. Nothing can be removed even if
    /// the answer were wrong, and the test answers **deny**.
    #[test]
    #[ignore = "talks to a real box: costs a wake and model calls"]
    fn an_approval_is_surfaced_and_answered_mid_turn() {
        let Ok(token) = std::env::var("FLOTTA_TOKEN") else {
            panic!("set FLOTTA_TOKEN to a box:chat token");
        };
        let settings = Settings {
            control_url: std::env::var("FLOTTA_CONTROL_URL").unwrap_or_default(),
            domain: std::env::var("FLOTTA_DOMAIN").unwrap_or_else(|_| "flotta.dev".into()),
        };
        let box_name = std::env::var("FLOTTA_BOX").unwrap_or_else(|_| "eng-a".into());

        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();

        runtime.block_on(async {
            let (mut socket, _, _) = connect(&settings, &box_name, &token)
                .await
                .unwrap_or_else(|e| panic!("connect failed: {}", e.detail()));

            // **The model is not the thing under test, and it is not
            // deterministic.** Asked to pipe a download into a shell it refuses
            // on its own, before the tool is ever called, and the gate never
            // fires — three attempts in a row did exactly that. A routine
            // cleanup reads as ordinary work to the model and is still flagged
            // by Hermes ("delete in root path"), measured escalating on three
            // runs out of three. The path does not exist, so even a wrongly
            // approved run removes nothing; the test answers deny regardless.
            const PROMPT: &str = "Please run this shell command with your terminal tool \
                and report the output: rm -rf /tmp/flotta-approval-probe";

            for attempt in 1..=3 {
                // A fresh session each time, so an approval left pending by an
                // earlier attempt cannot be the one this one answers.
                let session = create_session(&mut socket)
                    .await
                    .unwrap_or_else(|e| panic!("session.create failed: {}", e.detail()));

                let (tx, mut rx) = mpsc::channel(8);
                let mut deferred = VecDeque::new();
                let mut ping = tokio::time::interval(PING_EVERY);
                ping.tick().await;

                let seen: std::sync::Arc<std::sync::Mutex<Vec<ApprovalRequest>>> =
                    Default::default();
                let record = seen.clone();
                let started = tokio::time::Instant::now();

                let reply = one_turn(
                    &mut socket,
                    &session,
                    PROMPT,
                    &mut rx,
                    &mut deferred,
                    &mut ping,
                    |note| {
                        if let TurnNote::Approval(request) = note {
                            record.lock().unwrap().push(request.clone());
                            // What the window's Deny button does.
                            tx.try_send(Command::Approve {
                                request_id: request.request_id.clone(),
                                choice: "deny".into(),
                            })
                            .expect("the conversation channel had room for an answer");
                        }
                    },
                )
                .await
                .unwrap_or_else(|e| panic!("attempt {attempt}: the turn failed: {}", e.detail()));

                let elapsed = started.elapsed();
                let asked = seen.lock().unwrap().clone();
                println!(
                    "attempt {attempt}: {} approval(s), reply after {elapsed:?}",
                    asked.len()
                );

                if asked.is_empty() {
                    println!(
                        "  the model did not reach the gate: {:?}",
                        reply.chars().take(120).collect::<String>()
                    );
                    continue;
                }

                println!("  surfaced: {:#?}", asked[0]);
                println!("  reply: {:?}", reply.chars().take(200).collect::<String>());
                assert!(
                    asked[0].command.contains("flotta-approval-probe"),
                    "surfaced the wrong command: {:?}",
                    asked[0].command
                );
                assert!(asked[0].choices.contains(&"deny".to_string()));
                // The whole ticket: answered, the turn ends in seconds rather
                // than waiting out the gate's own timeout.
                assert!(
                    elapsed < std::time::Duration::from_secs(120),
                    "the turn still waited {elapsed:?} after the approval was answered"
                );
                assert!(
                    deferred.is_empty(),
                    "nothing but the answer should have arrived"
                );
                return;
            }
            panic!(
                "the model never reached the approval gate in three attempts; nothing was tested"
            );
        });
    }

    /// **FLOTTA-58's acceptance criterion, and it is live by necessity.**
    ///
    /// Changing an agent's standing instructions only takes effect across a
    /// conversation boundary, because Hermes renders a system prompt once per
    /// session and keeps it. Four separate bugs sat between "the button
    /// exists" and "the agent changes", and *every one of them was invisible
    /// to a green suite*: ssh that only works from a laptop, a fake backend
    /// that ignored the address it was given, a JSON shape that was assumed
    /// rather than measured, and a timestamp read once and then reused.
    ///
    /// What they have in common is that each lived at a boundary a test
    /// double was standing in for. So this one uses no doubles: the real
    /// control plane, the real box, and the same `attach` the window runs.
    ///
    /// ```sh
    /// FLOTTA_BOX=eng-r just app-live
    /// ```
    #[test]
    #[ignore = "talks to a real box and rewrites its instructions"]
    fn changed_instructions_start_a_fresh_conversation() {
        let Ok(token) = std::env::var("FLOTTA_TOKEN") else {
            panic!("set FLOTTA_TOKEN to a box:chat token");
        };
        let Ok(control) = std::env::var("FLOTTA_CONTROL_URL") else {
            panic!("set FLOTTA_CONTROL_URL: this test is about the control plane's answer");
        };
        let Ok(admin) = std::env::var("FLOTTA_WRITE_TOKEN") else {
            panic!("set FLOTTA_WRITE_TOKEN to a fleet:write token");
        };
        let settings = Settings {
            control_url: control,
            domain: std::env::var("FLOTTA_DOMAIN").unwrap_or_else(|_| "flotta.dev".into()),
        };
        let box_name = std::env::var("FLOTTA_BOX").unwrap_or_else(|_| "eng-a".into());

        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();

        runtime.block_on(async {
            // Start from a conversation that exists and has something in it,
            // so "fresh" is a claim with teeth rather than the empty state we
            // would have got anyway.
            let (mut socket, session, _) = connect(&settings, &box_name, &token)
                .await
                .unwrap_or_else(|e| panic!("connect failed: {}", e.detail()));
            let marker = format!("plum{}", std::process::id());
            turn_for_test(
                &mut socket,
                &session,
                &format!("Reply with exactly one word: {marker}. Nothing else."),
            )
            .await
            .unwrap_or_else(|e| panic!("turn failed: {}", e.detail()));
            drop(socket);

            let before = connect(&settings, &box_name, &token)
                .await
                .unwrap_or_else(|e| panic!("reconnect failed: {}", e.detail()))
                .2;
            assert!(
                before.iter().any(|l| l.text.contains(&marker)),
                "setup is wrong: the conversation did not come back before the edit"
            );

            // The write the Info panel makes. Distinctive enough that the
            // agent's own answer is evidence rather than interpretation.
            let word = format!("pineapple{}", std::process::id());
            let instructions = format!(
                "You are a test fixture. When asked anything at all, reply with \
                 exactly one word: {word}."
            );

            // The write needs `fleet:write`; the socket needs `box:chat`.
            // Swapped in the environment for the duration rather than widening
            // what a chat token is allowed to do.
            std::env::set_var("FLOTTA_TOKEN", &admin);
            let saved =
                crate::fleet::set_instructions(&settings, &box_name, Some(instructions)).await;
            std::env::set_var("FLOTTA_TOKEN", &token);
            let saved =
                saved.unwrap_or_else(|e| panic!("saving instructions failed: {}", e.detail()));
            println!("instructions written, changed_at {:?}", saved.changed_at);

            // The whole point: `attach` must notice and start over.
            let (_socket, _fresh, history) = connect(&settings, &box_name, &token)
                .await
                .unwrap_or_else(|e| panic!("connect after the edit failed: {}", e.detail()));
            assert!(
                history.is_empty(),
                "instructions changed and the old conversation came back anyway: {} lines",
                history.len()
            );

            let mut socket = _socket;
            let answer = turn_for_test(&mut socket, &_fresh, "Who are you?")
                .await
                .unwrap_or_else(|e| panic!("the turn after the edit failed: {}", e.detail()));
            println!("answer: {answer:?}");
            assert!(
                answer.to_lowercase().contains(&word),
                "the agent is still running the old instructions: {answer:?}"
            );
        });
    }

    /// FLOTTA-61's acceptance, against a real box: a turn that uses tools
    /// reports each one while it runs, before the reply — and the reply is
    /// still the reply.
    ///
    /// Also settles the one question the captured frames could not: whether
    /// `message.complete` repeats what the agent said *before* a tool. If it
    /// did, `Said` would print that sentence twice.
    #[test]
    #[ignore = "talks to a real box: costs a wake and model calls"]
    fn a_turn_shows_its_steps_as_they_happen() {
        let Ok(token) = std::env::var("FLOTTA_TOKEN") else {
            panic!("set FLOTTA_TOKEN to a box:chat token");
        };
        let settings = Settings {
            control_url: std::env::var("FLOTTA_CONTROL_URL").unwrap_or_default(),
            domain: std::env::var("FLOTTA_DOMAIN").unwrap_or_else(|_| "flotta.dev".into()),
        };
        let box_name = std::env::var("FLOTTA_BOX").unwrap_or_else(|_| "eng-a".into());

        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();

        runtime.block_on(async {
            let (mut socket, _, _) = connect(&settings, &box_name, &token)
                .await
                .unwrap_or_else(|e| panic!("connect failed: {}", e.detail()));
            let session = create_session(&mut socket)
                .await
                .unwrap_or_else(|e| panic!("session.create failed: {}", e.detail()));

            let (_tx, mut rx) = mpsc::channel(1);
            let mut deferred = VecDeque::new();
            let mut ping = tokio::time::interval(PING_EVERY);
            ping.tick().await;

            let steps: std::sync::Arc<std::sync::Mutex<Vec<(Duration, Step)>>> = Default::default();
            let record = steps.clone();
            let started = tokio::time::Instant::now();

            let reply = one_turn(
                &mut socket,
                &session,
                "First say the sentence 'Checking the machine now.' Then use your terminal \
                 tool to run `uname -s`, and then run `false`. Finally answer in one \
                 short sentence.",
                &mut rx,
                &mut deferred,
                &mut ping,
                |note| {
                    if let TurnNote::Progress(step) = note {
                        record.lock().unwrap().push((started.elapsed(), step));
                    }
                },
            )
            .await
            .unwrap_or_else(|e| panic!("the turn failed: {}", e.detail()));
            let replied = started.elapsed();

            let steps = steps.lock().unwrap().clone();
            for (at, step) in &steps {
                match step {
                    Step::Text { .. } | Step::Reasoning { .. } => {}
                    other => println!("{:>6.2}s {other:?}", at.as_secs_f64()),
                }
            }
            println!("{:>6.2}s reply {reply:?}", replied.as_secs_f64());

            let tools: Vec<_> = steps
                .iter()
                .filter_map(|(at, s)| match s {
                    Step::ToolFinished { failed, .. } => Some((*at, *failed)),
                    _ => None,
                })
                .collect();
            assert!(
                !tools.is_empty(),
                "no tool step was reported before the reply"
            );
            assert!(
                tools.iter().all(|(at, _)| *at < replied),
                "tool steps must arrive while the turn runs, not after it"
            );
            assert!(
                steps
                    .iter()
                    .any(|(_, s)| matches!(s, Step::ToolStarted { .. })),
                "a finished tool was never shown starting"
            );
            // `false` exits 1. If the model ran it, the window must say it failed.
            let ran_false = steps.iter().any(
                |(_, s)| matches!(s, Step::ToolStarted { detail, .. } if detail.trim() == "false"),
            );
            if ran_false {
                assert!(
                    tools.iter().any(|(_, failed)| *failed),
                    "`false` ran and nothing was shown as failed"
                );
            }
            assert!(
                steps.iter().any(|(_, s)| matches!(s, Step::Text { .. })),
                "the answer did not stream"
            );
            for (_, step) in &steps {
                if let Step::Said { text } = step {
                    assert!(
                        !reply.contains(text.as_str()),
                        "the reply repeats narration already shown as its own line: {text:?}"
                    );
                }
            }
        });
    }

    /// FLOTTA-61, part 2, against a real box: asked which repositories it can
    /// use, an agent answers from `flotta-repos` in at most two tool calls —
    /// loading the skill, then running the command — and names what the
    /// control plane says it is granted.
    ///
    /// Before, eng-r took nine model calls and seven shell commands, three of
    /// which failed. **Needs a fleet built from this branch** — the skill and
    /// the command ship in the box image.
    #[test]
    #[ignore = "talks to a real box: costs a wake and model calls"]
    fn an_agent_knows_which_repositories_it_may_use() {
        let Ok(token) = std::env::var("FLOTTA_TOKEN") else {
            panic!("set FLOTTA_TOKEN to a box:chat + fleet:read token");
        };
        let settings = Settings {
            control_url: std::env::var("FLOTTA_CONTROL_URL").unwrap_or_default(),
            domain: std::env::var("FLOTTA_DOMAIN").unwrap_or_else(|_| "flotta.dev".into()),
        };
        let box_name = std::env::var("FLOTTA_BOX").unwrap_or_else(|_| "eng-a".into());

        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();

        runtime.block_on(async {
            let granted = crate::fleet::list_repos(&settings, &box_name)
                .await
                .unwrap_or_else(|e| panic!("could not read the grants: {}", e.detail()));
            println!("control plane says {box_name} is granted {granted:?}");

            let (mut socket, _, _) = connect(&settings, &box_name, &token)
                .await
                .unwrap_or_else(|e| panic!("connect failed: {}", e.detail()));
            // A fresh session: the skill index is rendered when a session starts.
            let session = create_session(&mut socket)
                .await
                .unwrap_or_else(|e| panic!("session.create failed: {}", e.detail()));

            let (_tx, mut rx) = mpsc::channel(1);
            let mut deferred = VecDeque::new();
            let mut ping = tokio::time::interval(PING_EVERY);
            ping.tick().await;
            let steps: std::sync::Arc<std::sync::Mutex<Vec<Step>>> = Default::default();
            let record = steps.clone();
            let started = tokio::time::Instant::now();

            let reply = one_turn(
                &mut socket,
                &session,
                "Which GitHub repositories do you have access to?",
                &mut rx,
                &mut deferred,
                &mut ping,
                |note| {
                    if let TurnNote::Progress(step @ Step::ToolStarted { .. }) = note {
                        record.lock().unwrap().push(step);
                    }
                },
            )
            .await
            .unwrap_or_else(|e| panic!("the turn failed: {}", e.detail()));

            let tools = steps.lock().unwrap().clone();
            println!("{:.1}s, tools: {tools:#?}", started.elapsed().as_secs_f64());
            println!("reply: {reply}");

            assert!(
                tools.iter().any(
                    |s| matches!(s, Step::ToolStarted { detail, .. } if detail.contains("flotta-repos"))
                ),
                "the agent never ran flotta-repos"
            );
            assert!(
                tools.len() <= 2,
                "{} tool calls to answer a question one command answers",
                tools.len()
            );
            for repo in &granted {
                assert!(
                    reply.to_lowercase().contains(&repo.to_lowercase()),
                    "the reply leaves out {repo}, which is granted"
                );
            }
            if granted.is_empty() {
                let lower = reply.to_lowercase();
                assert!(
                    lower.contains("no") || lower.contains("none"),
                    "no grants, and the reply does not say so: {reply}"
                );
            }
        });
    }
}
