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

**A custom agent runs itself** (M17.3). An agent created with the image
`local:<dir>` is that directory's own `agent.py`, the agency's code under the
box contract, rather than a stand-in: its dependencies are installed into the
box's directory by `uv sync`, and it is told where to listen
(`$FLOTTA_HOST`/`$FLOTTA_PORT`) and where its volume is (`$FLOTTA_DATA`). It is
up when `GET /healthz` answers 200 to its own secret, as the door asks.
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
#: The model's *name* it does get: it says it, so a change of model can be seen
#: to reach it (FLOTTA-85).
_KEPT_SECRETS = (standin.USERNAME_ENV, standin.PASSWORD_ENV, standin.MODEL_ENV)
#: What a stand-in inherits from whoever started it: enough to run Python.
_INHERITED_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
#: What `uv sync` runs with: enough to find Python and its cache, and no secret.
_UV_ENV = (*_INHERITED_ENV, "UV_CACHE_DIR", "UV_PYTHON", "XDG_CACHE_HOME", "XDG_DATA_HOME")
START_TIMEOUT_S = 15.0
#: A custom agent imports its own libraries before it answers (LangGraph and
#: LangChain take a while from a cold cache), so it is given longer.
CUSTOM_START_TIMEOUT_S = 90.0
#: A custom agent's first start installs its dependencies, LangGraph and all.
INSTALL_TIMEOUT_S = 600.0
#: What a custom agent is: where its code is. Beside its endpoint.
RUNTIME_FILE = "runtime.json"
#: The marker on a custom agent's command line, so a stop can tell it is this
#: agent's process before signalling it, as `--name` does for a stand-in.
CUSTOM_MARK = "--flotta-box"
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
    for module, flag in (("flotta.backends.standin", "--name"), (None, CUSTOM_MARK)):
        if (module is not None and module not in listed) or flag not in listed:
            continue
        at = listed.index(flag) + 1
        if at < len(listed) and listed[at] == name:
            return True
    return False


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

    def _custom(self, target: str) -> Path | None:
        """A custom agent's code, if this box is one."""
        path = self._read(target, RUNTIME_FILE).get("path")
        return Path(path) if isinstance(path, str) and path else None

    def _probe(self, target: str) -> dict | None:
        """What answers on this box's port, if it is this box's stand-in."""
        found = self.existing_endpoint(self._name(target))
        if not found:
            return None
        host, port, name = parse_endpoint(found)
        if self._custom(name) is not None:
            return self._probe_custom(name, host, port)
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

    def _probe_custom(self, name: str, host: str, port: int) -> dict | None:
        """A custom agent is up when it answers its own secret, and it is this
        agent when the process recorded for it is still it."""
        from flotta.provision import BOX_SECRET_ENV

        secret = self._read(name, "secrets.json").get(BOX_SECRET_ENV, "")
        request = urllib.request.Request(
            f"http://{host}:{port}/healthz", headers={"Authorization": f"Bearer {secret}"}
        )
        try:
            with _DIRECT.open(request, timeout=2.0) as response:
                if response.status != 200:
                    return None
        except (OSError, ValueError, urllib.error.URLError):
            return None
        pid = self._recorded_pid(name)
        return None if pid is None else {"box": name, "pid": pid}

    # -- the protocol -----------------------------------------------------------

    def create(self, spec: BoxSpec) -> BoxHandle:
        name = self._name(spec.name)
        found = self.existing_endpoint(name)
        directory = self._dir(name)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "hermes").mkdir(exist_ok=True)
        endpoint = found or f"{SCHEME}://127.0.0.1:{_free_port()}/{name}"
        # A Hermes agent's image means nothing here (it is a stand-in); a custom
        # agent's is the code it runs, and must be on this machine.
        custom = _local_path(spec.image) if spec.env.get("FLOTTA_RUNTIME") == "custom" else None
        if custom is not None:
            self._write(name, RUNTIME_FILE, {"path": str(custom)})
            (directory / "data").mkdir(exist_ok=True)
        self._write(name, "env.json", dict(spec.env))
        # A custom agent is the agency's own code and is given what it was
        # given on Fly; a stand-in, only what it reads.
        kept = dict(spec.secrets) if custom is not None else _kept(spec.secrets)
        self._write(name, "secrets.json", kept, private=True)
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
        custom = self._custom(name)
        if custom is not None:
            command = self._install(name, custom, env)
            env.update(
                {
                    "FLOTTA_HOST": host,
                    "FLOTTA_PORT": str(port),
                    "FLOTTA_DATA": str(directory / "data"),
                }
            )
        else:
            command = [
                self.python,
                "-m",
                "flotta.backends.standin",
                "--name",
                name,
                "--home",
                str(directory),
                "--port",
                str(port),
            ]
        with open(directory / "agent.log", "ab") as log:
            child = subprocess.Popen(
                command,
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
        deadline = time.monotonic() + (
            CUSTOM_START_TIMEOUT_S if custom is not None else START_TIMEOUT_S
        )
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

    def _install(self, name: str, code: Path, env: dict[str, str]) -> list[str]:
        """A custom agent's dependencies, into the box's own directory, and the
        command that runs it. The agent's directory is left as it was found: a
        committed lockfile is used as it is (`--frozen`), and uv is given none of
        the box's secrets, which a dependency's build could otherwise read."""
        uv = shutil.which("uv")
        if uv is None:
            raise BackendError(
                "a local custom agent needs uv on the PATH: https://docs.astral.sh/uv/"
            )
        if not (code / "agent.py").is_file():
            raise BackendError(f"{code} has no agent.py")
        venv = self._dir(name) / "venv"
        with open(self._dir(name) / "agent.log", "ab") as log:
            try:
                frozen = ["--frozen"] if (code / "uv.lock").is_file() else []
                inherited = {k: os.environ[k] for k in _UV_ENV if k in os.environ}
                installed = subprocess.run(
                    [uv, "sync", "--project", str(code), "--no-dev", "--quiet", *frozen],
                    env={**inherited, "UV_PROJECT_ENVIRONMENT": str(venv)},
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=INSTALL_TIMEOUT_S,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise BackendError(f"installing {name!r}'s dependencies took too long") from exc
        if installed.returncode != 0:
            raise BackendError(
                f"installing {name!r}'s dependencies failed; see {self._dir(name) / 'agent.log'}"
            )
        return [str(venv / "bin" / "python"), str(code / "agent.py"), CUSTOM_MARK, name]

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
            # And reaped once it has died, or it lingers as a zombie of this process.
            killed = time.monotonic() + 1.0
            while time.monotonic() < killed and _alive(pid, child):
                time.sleep(0.02)
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

    def leftover(self, name: str) -> bool:
        """Whether anything of this box is still here (FLOTTA-30): its directory,
        which `create` would reuse and `destroy` removes."""
        return self._dir(self._name(name)).exists()

    def destroy(self, box_id: str) -> None:
        try:
            name = self._name(box_id)
        except BackendError:
            return
        self.stop(name)
        shutil.rmtree(self._dir(name), ignore_errors=True)

    def apply_secrets(self, box_id: str, secrets: dict[str, str]) -> None:
        name = self._name(box_id)
        # A custom agent keeps every one, as `create` gave it: a rotated door
        # secret or gateway token dropped here would leave it on the old ones.
        given = dict(secrets) if self._custom(name) is not None else _kept(secrets)
        kept = {**self._read(name, "secrets.json"), **given}
        # Asked before the write: a custom agent is probed with its own secret,
        # and the one it is still running on is the old one.
        running = self._probe(name) is not None
        self._write(name, "secrets.json", kept, private=True)
        # A box reads its environment once, at boot, here as on Fly.
        if running:
            self.stop(name)
            self.start(name)

    def remove_secrets(self, box_id: str, names: list[str]) -> None:
        name = self._name(box_id)
        kept = {k: v for k, v in self._read(name, "secrets.json").items() if k not in names}
        running = self._probe(name) is not None
        self._write(name, "secrets.json", kept, private=True)
        if running:
            self.stop(name)
            self.start(name)

    def apply_env(self, box_id: str, env: dict[str, str]) -> None:
        name = self._name(box_id)
        self._write(name, "env.json", {**self._read(name, "env.json"), **env})
        if self._probe(name) is not None:
            self.stop(name)
            self.start(name)

    def reimage(self, box_id: str, image: str) -> None:
        """A custom agent onto other code: `local:<dir>`, restarted if it was up."""
        name = self._name(box_id)
        if self._custom(name) is None:
            raise NotSupported("a local stand-in has no image to replace")
        custom = _local_path(image)
        running = self._probe(name) is not None
        if running:
            self.stop(name)
        self._write(name, RUNTIME_FILE, {"path": str(custom)})
        if running:
            self.start(name)

    def check_resize(self, box_id: str, memory_mb: int) -> None:
        raise NotSupported(
            "a local stand-in is a process on this machine; it has no size to change"
        )

    def resize(
        self,
        box_id: str,
        memory_mb: int,
        *,
        env: dict[str, str] | None = None,
        at_least: bool = False,
    ) -> int | None:
        # Refused rather than recorded: a process has no VM to size, and saying
        # it now has 4 GB would be a number the window shows and nothing holds.
        raise NotSupported(
            "a local stand-in is a process on this machine; it has no size to change"
        )

    def exec(self, box_id: str, command: str, *, timeout_s: int = 300) -> ExecResult:
        """No shell, but the two commands `set_instructions` sends, and nothing else.

        A local agent's instructions have to be changeable for the web app's
        agent page to be tried for $0 (FLOTTA-84). Each command is matched
        whole against the shape `set_instructions` builds, and acts on this
        agent's own `SOUL.md`; anything else is still refused.
        """
        import base64
        import binascii

        from flotta.provision import SOUL_PATH

        path = standin.soul_path(self._dir(box_id))
        if command == f"rm -f {SOUL_PATH}":
            path.unlink(missing_ok=True)
            return ExecResult(exit_code=0, stdout="", stderr="")
        rules = self._rules(box_id, command)
        if rules is not None:
            return rules
        written = re.fullmatch(
            rf"printf %s ([A-Za-z0-9+/=]+) \| base64 -d > {re.escape(SOUL_PATH)}", command
        )
        if written is None:
            raise NotSupported("a local stand-in has no shell")
        try:
            content = base64.b64decode(written.group(1), validate=True)
        except binascii.Error as exc:
            raise BackendError(f"not base64: {exc}") from exc
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return ExecResult(exit_code=0, stdout="", stderr="")

    def _rules(self, box_id: str, command: str) -> ExecResult | None:
        """`set_rules`'s command (FLOTTA-124), into this agent's own `config.yaml`.

        Only when the script it carries is `flotta.box.rules` exactly, which then
        runs here as itself rather than as whatever the command held.
        """
        import base64
        import json
        from pathlib import Path

        from flotta.box import rules as box_rules
        from flotta.provision import BOX_PYTHON, HERMES_HOME

        shape = re.fullmatch(
            rf"printf %s ([A-Za-z0-9+/=]+) \| base64 -d \| {re.escape(BOX_PYTHON)} - "
            rf"{re.escape(HERMES_HOME)} ([A-Za-z0-9+/=]+)",
            command,
        )
        if shape is None:
            return None
        if base64.b64decode(shape.group(1)) != Path(box_rules.__file__).read_bytes():
            raise NotSupported("a local stand-in runs no script but Flotta's rules writer")
        try:
            changes = json.loads(base64.b64decode(shape.group(2)))
            home = standin.soul_path(self._dir(box_id)).parent
            home.mkdir(parents=True, exist_ok=True)
            said = box_rules.apply(home, changes)
        except (OSError, ValueError) as exc:
            return ExecResult(exit_code=1, stdout="", stderr=f"rules not written: {exc}")
        return ExecResult(exit_code=0, stdout=said, stderr="")

    def state(self, box_id: str) -> str:
        name = self._name(box_id)
        if not self._dir(name).exists():
            return "gone"
        return "started" if self._probe(name) is not None else "stopped"

    def inspect(self, box_id: str) -> MachineInfo:
        name = self._name(box_id)
        env = self._read(name, "env.json")
        return MachineInfo(
            state=self.state(name),
            machine_id=name,
            app=SCHEME,
            region=SCHEME,
            volume_path=str(
                self._dir(name) / ("data" if self._custom(name) is not None else "hermes")
            ),
            control_url=(env.get("FLOTTA_CONTROL_URL") or None),
            env_box_id=(env.get("FLOTTA_BOX_ID") or None),
            git_author_name=(env.get("FLOTTA_GIT_AUTHOR_NAME") or None),
            git_author_email=(env.get("FLOTTA_GIT_AUTHOR_EMAIL") or None),
        )


def _local_path(image: str | None) -> Path:
    """`local:<dir>`'s directory. Any other image is refused: nothing on this
    machine can boot a container."""
    from flotta.store import LOCAL_IMAGE_PREFIX

    if not image:
        raise BackendError("a custom agent on a local fleet needs local:<dir> as its image")
    if not image.startswith(LOCAL_IMAGE_PREFIX):
        raise BackendError(f"a local fleet runs an agent's own code as local:<dir>, not {image!r}")
    return Path(image[len(LOCAL_IMAGE_PREFIX) :]).expanduser().resolve()


def _kept(secrets: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in secrets.items() if k in _KEPT_SECRETS}
