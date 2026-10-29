"""`local://`: every agent a process on this machine (M12.6).

The second implementation of `Backend`, and the first one that costs nothing.
Each box is a stand-in for Hermes (`flotta.backends.standin`) listening on a
loopback port, with a directory that plays the part of its volume. With
`$FLOTTA_BACKEND=local`, creating an agent through the fleet API, waking it
through the door and talking to it all happen here, for $0, with no Fly and
no model.

**The endpoint carries the address:** `local://127.0.0.1:<port>/<name>`. The
door turns it into where to proxy, the same way it turns `fly://app/machine`
into `<machine>.vm.<app>.internal`, and `parse_endpoint` refuses any host that
is not loopback. That is the promise this backend exists to keep: nothing on
the local path opens a connection off the machine, and a stored endpoint is
not allowed to be the exception.

**Stateless, as the protocol asks.** What a box is lives in its directory
(`$FLOTTA_LOCAL_HOME/<name>/`): its endpoint, its login, its conversations.
Whether it is up is asked of the port, never of a pid file: the stand-in
answers `GET /` with its name and pid, so a port held by anything else reads
as "not this agent" rather than as a running one.

What a stand-in cannot do, it says: there is no shell to `exec` in, no image
to `reimage`, and no memory snapshot, so `suspend` is `NotSupported` and
`pause` stops it instead.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from flotta.backend import BackendError, BoxHandle, BoxSpec, ExecResult, MachineInfo, NotSupported
from flotta.backends import standin

SCHEME = "local"
HOME_ENV = "FLOTTA_LOCAL_HOME"
DEFAULT_HOME = Path("~/.flotta/local/boxes")
#: A box name is a DNS label, which also makes it a safe directory name. The
#: check is repeated here because a path is the one place a `..` would matter.
_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
#: The only variables a stand-in reads, and so the only secrets written to its
#: directory. A real box is also handed the fleet's model key and its own
#: token; a stand-in calls no model and fetches nothing, so it gets neither.
_KEPT_SECRETS = (standin.USERNAME_ENV, standin.PASSWORD_ENV)
#: What a stand-in inherits from whoever started it: enough to run Python.
_INHERITED_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
START_TIMEOUT_S = 15.0
#: Asking the port ignores `$http_proxy`: a proxy would carry a loopback
#: question off the machine and answer it for whatever is listening there.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
STOP_TIMEOUT_S = 5.0


def parse_endpoint(endpoint: str) -> tuple[str, int, str]:
    """`local://127.0.0.1:41234/eng-a` → `("127.0.0.1", 41234, "eng-a")`.

    Refuses a host that is not a loopback address. The door proxies to what
    this returns, so a `local://` row naming another machine would turn a
    free local agent into a way to send traffic anywhere.
    """
    parts = urlsplit(endpoint)
    name = parts.path.strip("/")
    if parts.scheme != SCHEME or not parts.hostname or not parts.port or not _NAME.match(name):
        raise BackendError(f"not a local endpoint: {endpoint!r}")
    host = parts.hostname
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise BackendError(f"a local endpoint must be on loopback, not {host!r}")
    return host, parts.port, name


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


#: Where a stand-in's pid is kept, beside its endpoint, for any backend to find.
PID_FILE = "pid"


def _is_standin(pid: int, name: str) -> bool:
    """Whether `pid` is the stand-in for `name`: pids are reused, and a stop
    must never signal whatever process has one now."""
    try:
        listed = subprocess.run(
            ["ps", "-ww", "-o", "args=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        ).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        return False
    if "flotta.backends.standin" not in listed or "--name" not in listed:
        return False
    at = listed.index("--name") + 1
    return at < len(listed) and listed[at] == name


def _alive(pid: int, child: subprocess.Popen | None) -> bool:
    """Whether the process is still there, reaping it if it is ours and has exited.

    The control plane makes a backend per request, so the one stopping an
    agent is usually not the one that started it and has no `Popen` for it.
    The process is still this process's child, though, and one that has
    exited stays a zombie, which `kill(pid, 0)` reports as alive: every stop
    waited its whole timeout and left one behind. `waitpid` reaps it.
    """
    if child is not None:
        return child.poll() is None
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass  # not ours: ask the kernel whether it exists
    else:
        return reaped == 0
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # someone else's: it exists
        return True
    return True


class LocalBackend:
    scheme = SCHEME
    #: A stand-in boots from no image, so the fleet image is not looked up for
    #: it — that lookup asks Fly (`provision.create_box`).
    takes_image = False

    def __init__(self, home: Path | str | None = None, *, python: str | None = None) -> None:
        if home is None:
            home = os.environ.get(HOME_ENV) or DEFAULT_HOME
        self.home = Path(home).expanduser()
        self.python = python or sys.executable
        #: Children this process started, so a stop can reap what it killed.
        self._children: dict[str, subprocess.Popen] = {}

    # -- where a box lives ------------------------------------------------------

    def _name(self, target: str) -> str:
        """A box name from an endpoint or a bare name, as every verb takes."""
        if "://" in target:
            return parse_endpoint(target)[2]
        if not _NAME.match(target):
            raise BackendError(f"not a box name: {target!r}")
        return target

    def _dir(self, target: str) -> Path:
        return self.home / self._name(target)

    def _read(self, target: str, file: str) -> dict:
        try:
            return json.loads((self._dir(target) / file).read_text())
        except FileNotFoundError:
            return {}

    def _write(self, target: str, file: str, value: dict, *, private: bool = False) -> None:
        path = self._dir(target) / file
        # Created with its mode rather than chmod-ed after: a password should
        # never sit in a world-readable file, even for a moment.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
        with os.fdopen(fd, "w") as out:
            json.dump(value, out)

    def existing_endpoint(self, name: str | None = None) -> str | None:
        if name is None:
            return None
        try:
            return (self._dir(name) / "endpoint").read_text().strip() or None
        except (FileNotFoundError, BackendError):
            return None

    def endpoint(self, box_id: str) -> str:
        found = self.existing_endpoint(self._name(box_id))
        if not found:
            raise BackendError(f"no local box {box_id!r}")
        return found

    # -- asking the port --------------------------------------------------------

    def _probe(self, target: str) -> dict | None:
        """What answers on this box's port, if it is this box's stand-in."""
        found = self.existing_endpoint(self._name(target))
        if not found:
            return None
        host, port, name = parse_endpoint(found)
        try:
            with _DIRECT.open(f"http://{host}:{port}/", timeout=2.0) as response:
                body = json.loads(response.read())
        except (OSError, ValueError, urllib.error.URLError):
            return None
        if not isinstance(body, dict):
            return None
        if body.get("agent") != standin.KIND or body.get("box") != name:
            return None
        return body

    # -- the protocol -----------------------------------------------------------

    def create(self, spec: BoxSpec) -> BoxHandle:
        name = self._name(spec.name)
        found = self.existing_endpoint(name)
        directory = self._dir(name)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "hermes").mkdir(exist_ok=True)
        endpoint = found or f"{SCHEME}://127.0.0.1:{_free_port()}/{name}"
        self._write(name, "env.json", dict(spec.env))
        self._write(name, "secrets.json", _kept(spec.secrets), private=True)
        (directory / "endpoint").write_text(endpoint)
        return BoxHandle(id=name, endpoint=endpoint, adopted=found is not None)

    def start(self, box_id: str) -> None:
        if self._probe(box_id) is not None:
            return
        name = self._name(box_id)
        host, port, _ = parse_endpoint(self.endpoint(name))
        directory = self._dir(name)
        env = {k: os.environ[k] for k in _INHERITED_ENV if k in os.environ}
        env.update(self._read(name, "env.json"))
        env.update(self._read(name, "secrets.json"))
        with open(directory / "agent.log", "ab") as log:
            child = subprocess.Popen(
                [
                    self.python,
                    "-m",
                    "flotta.backends.standin",
                    "--name",
                    name,
                    "--home",
                    str(directory),
                    "--port",
                    str(port),
                ],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                # Its own session, so a Ctrl-C meant for the control plane does
                # not take every agent with it: a box outlives whoever woke it.
                start_new_session=True,
            )
        self._children[name] = child
        (directory / PID_FILE).write_text(str(child.pid))
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._probe(name) is not None:
                return
            if child.poll() is not None:
                raise BackendError(
                    f"the local agent {name!r} exited ({child.returncode}); "
                    f"see {directory / 'agent.log'}"
                )
            time.sleep(0.1)
        raise BackendError(f"the local agent {name!r} did not answer on {host}:{port}")

    def suspend(self, box_id: str) -> None:
        raise NotSupported("a local stand-in has no memory snapshot; it is stopped instead")

    def stop(self, box_id: str) -> None:
        name = self._name(box_id)
        running = self._probe(name)
        # A hung agent does not answer the probe, and is the one that most
        # needs stopping. The backend that started it knows its process; any
        # other (the control plane makes one per request) has the pid it
        # recorded, which counts only while that process is still this agent.
        child = self._children.pop(name, None)
        if running is not None:
            pid: int | None = int(running["pid"])
        elif child is not None and child.poll() is None:
            pid = child.pid
        else:
            pid = self._recorded_pid(name)
        if pid is None:
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        # Until the process has gone, not until its port goes quiet: it stops
        # listening first, and a stop that returned then left it holding every
        # socket open on it, answering nothing.
        deadline = time.monotonic() + STOP_TIMEOUT_S
        while time.monotonic() < deadline and _alive(pid, child):
            time.sleep(0.05)
        if _alive(pid, child):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        if child is not None:
            child.wait(timeout=STOP_TIMEOUT_S)
        with contextlib.suppress(FileNotFoundError):
            (self._dir(name) / PID_FILE).unlink()

    def _recorded_pid(self, name: str) -> int | None:
        """The pid this agent's stand-in started with, if that process is still it."""
        try:
            pid = int((self._dir(name) / PID_FILE).read_text().strip())
        except (FileNotFoundError, ValueError):
            return None
        return pid if _is_standin(pid, name) else None

    def destroy(self, box_id: str) -> None:
        try:
            name = self._name(box_id)
        except BackendError:
            return
        self.stop(name)
        shutil.rmtree(self._dir(name), ignore_errors=True)

    def apply_secrets(self, box_id: str, secrets: dict[str, str]) -> None:
        name = self._name(box_id)
        kept = {**self._read(name, "secrets.json"), **_kept(secrets)}
        self._write(name, "secrets.json", kept, private=True)
        # A box reads its environment once, at boot, here as on Fly.
        if self._probe(name) is not None:
            self.stop(name)
            self.start(name)

    def reimage(self, box_id: str, image: str) -> None:
        raise NotSupported("a local stand-in has no image to replace")

    def exec(self, box_id: str, command: str, *, timeout_s: int = 300) -> ExecResult:
        raise NotSupported("a local stand-in has no shell")

    def state(self, box_id: str) -> str:
        name = self._name(box_id)
        if not self._dir(name).exists():
            return "gone"
        return "started" if self._probe(name) is not None else "stopped"

    def inspect(self, box_id: str) -> MachineInfo:
        name = self._name(box_id)
        return MachineInfo(
            state=self.state(name),
            machine_id=name,
            app=SCHEME,
            region=SCHEME,
            volume_path=str(self._dir(name) / "hermes"),
        )


def _kept(secrets: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in secrets.items() if k in _KEPT_SECRETS}
