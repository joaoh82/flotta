"""The credential helper, which git drives and nobody watches.

Every failure here is invisible at the moment it happens: git prints its own
authentication error, the agent reports that a clone failed, and the reason
this helper had is three layers down. So the behaviours worth pinning are the
quiet ones — what it prints, what it does *not* print, and that it never fails
loudly enough to break an anonymous clone that would otherwise work.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from flotta.box import git_credential as gc


def _run(argv, stdin_text, env=None, fetch=None):
    """Drive `main` end to end. Returns `(code, stdout, stderr)`."""
    out, err = io.StringIO(), io.StringIO()
    code = gc.main(
        argv,
        stdin=io.StringIO(stdin_text),
        stdout=out,
        stderr=err,
        env=env if env is not None else {},
        fetch=fetch,
    )
    return code, out.getvalue(), err.getvalue()


def _token_for(box_id: str) -> str:
    """A real token, because the helper reads the box out of it now."""
    from flotta.auth import SCOPE_GIT_CREDENTIAL, box_subject, mint

    return mint(subject=box_subject(box_id), scopes={SCOPE_GIT_CREDENTIAL}, key="k" * 32)


ENV = {
    gc.CONTROL_URL_ENV: "https://control.example",
    gc.BOX_TOKEN_ENV: _token_for("b-123"),
}

REQUEST = "protocol=https\nhost=github.com\npath=joaoh82/flotta.git\n\n"


# -- the protocol -----------------------------------------------------------


def test_a_blank_line_ends_the_request():
    """git may keep the pipe open; a helper that reads to EOF would hang."""
    fields = gc.parse_request(io.StringIO("host=github.com\n\npath=leaked\n"))
    assert fields == {"host": "github.com"}


def test_unknown_keys_are_kept_not_rejected():
    """git has added fields to this protocol before; breaking on one is a bug."""
    fields = gc.parse_request(io.StringIO("host=github.com\nwwwauth[]=Basic\n\n"))
    assert fields["wwwauth[]"] == "Basic"


def test_a_value_containing_an_equals_sign_survives():
    fields = gc.parse_request(io.StringIO("password=a=b=c\n\n"))
    assert fields["password"] == "a=b=c"


@pytest.mark.parametrize(
    "path,expected",
    [
        ("joaoh82/flotta.git", "joaoh82/flotta"),
        ("joaoh82/flotta", "joaoh82/flotta"),
        ("/joaoh82/flotta/", "joaoh82/flotta"),
        ("joaoh82/Flotta.git", "joaoh82/flotta"),
        # git can ask about a path deeper than the repository root.
        ("joaoh82/flotta/info/refs", "joaoh82/flotta"),
    ],
)
def test_the_repository_is_read_out_of_the_path(path, expected):
    assert gc.repo_from_request({"host": "github.com", "path": path}) == expected


def test_a_non_github_host_is_not_ours():
    """Its own exception type, because it is the one that must stay silent."""
    with pytest.raises(gc.NotOurs):
        gc.repo_from_request({"host": "gitlab.com", "path": "a/b"})


def test_no_path_names_the_config_that_is_missing():
    """`useHttpPath` is the setting that makes per-repository grants possible."""
    with pytest.raises(gc.HelperError, match="useHttpPath"):
        gc.repo_from_request({"host": "github.com"})


# -- what reaches git -------------------------------------------------------


def test_a_granted_repository_yields_a_credential():
    code, out, err = _run(
        ["get"], REQUEST, env=ENV, fetch=lambda repo, env: ("x-access-token", "ghs_secret")
    )
    assert code == 0
    assert out == "username=x-access-token\npassword=ghs_secret\n"
    assert err == ""


def test_the_repository_asked_about_is_the_one_in_the_request():
    seen = []
    _run(["get"], REQUEST, env=ENV, fetch=lambda repo, env: seen.append(repo) or ("u", "p"))
    assert seen == ["joaoh82/flotta"]


@pytest.mark.parametrize("verb", ["store", "erase", "", "some-future-verb"])
def test_only_get_produces_output(verb):
    """`store`/`erase` are cache verbs and there is no cache. Doing nothing is
    the correct implementation, not a stub."""
    code, out, err = _run(
        [verb] if verb else [], REQUEST, env=ENV, fetch=lambda repo, env: ("u", "p")
    )
    assert (code, out, err) == (0, "", "")


# -- failure is quiet, and never breaks a clone that would have worked -------


def test_a_non_github_request_says_nothing_at_all():
    """git asks every configured helper about every host. A line here would
    print on every request to a private package registry."""
    code, out, err = _run(["get"], "protocol=https\nhost=gitlab.com\npath=a/b\n\n", env=ENV)
    assert (code, out, err) == (0, "", "")


def test_an_unconfigured_box_explains_itself_and_still_exits_zero():
    """Exit 0 with no credential: git then behaves as if no helper existed, so
    an anonymous clone of a public repository still works."""
    code, out, err = _run(["get"], REQUEST, env={})
    assert code == 0
    assert out == ""
    assert "box-identity" in err
    for name in (gc.CONTROL_URL_ENV, gc.BOX_TOKEN_ENV):
        assert name in err


def test_a_refusal_passes_the_control_planes_own_words_through():
    """The 403 already names the command that fixes it. Paraphrasing loses that."""

    def refuse(repo, env):
        raise gc.HelperError("control plane refused (403): Grant it with `flotta repo grant`")

    code, out, err = _run(["get"], REQUEST, env=ENV, fetch=refuse)
    assert (code, out) == (0, "")
    assert "flotta repo grant" in err


def test_the_token_never_reaches_stderr():
    """The one output nobody inspects is the one a secret would leak into."""

    def boom(repo, env):
        raise gc.HelperError(f"cannot reach the control plane at {env[gc.CONTROL_URL_ENV]}")

    _, _, err = _run(["get"], REQUEST, env=ENV, fetch=boom)
    assert ENV[gc.BOX_TOKEN_ENV] not in err


# -- the request the helper actually makes ----------------------------------


def test_the_credential_url_is_the_endpoint_part_one_built():
    assert (
        gc.credential_url("https://control.example/", "b-123")
        == "https://control.example/api/boxes/b-123/git-credential"
    )


def test_fetch_sends_a_bearer_token_and_the_repository(monkeypatch):
    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"username": "x-access-token", "password": "ghs_x"}).encode()

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        captured["body"] = json.loads(request.data.decode())
        return _Response()

    monkeypatch.setattr(gc.urllib.request, "urlopen", fake_urlopen)
    assert gc.fetch_credential("joaoh82/flotta", env=ENV) == ("x-access-token", "ghs_x")
    assert captured["url"] == "https://control.example/api/boxes/b-123/git-credential"
    assert captured["auth"] == f"Bearer {ENV[gc.BOX_TOKEN_ENV]}"
    assert captured["body"] == {"repo": "joaoh82/flotta"}


def test_an_http_error_keeps_the_control_planes_detail(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url,
            403,
            "Forbidden",
            {},
            io.BytesIO(json.dumps({"detail": "box 'eng-a' is not granted 'x/y'"}).encode()),
        )

    monkeypatch.setattr(gc.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(gc.HelperError, match="is not granted"):
        gc.fetch_credential("x/y", env=ENV)


def test_an_empty_password_is_refused_rather_than_handed_to_git(monkeypatch):
    """git would take `password=` at face value and retry forever."""

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"username": "x-access-token", "password": ""}).encode()

    monkeypatch.setattr(gc.urllib.request, "urlopen", lambda r, timeout=None: _Response())
    with pytest.raises(gc.HelperError, match="no credential"):
        gc.fetch_credential("x/y", env=ENV)


# -- the box a token speaks for is the box it addresses ---------------------


def test_the_box_is_read_out_of_the_token_not_the_environment(monkeypatch):
    """Found live. Adopting an existing machine rotates its secrets but not its
    environment, so a box held a token for one id while `$FLOTTA_BOX_ID` still
    named another. Addressing the environment's box with the token's identity
    is refused by the subject check — and the fleet had already recorded the
    identity as minted, so nothing pointed at the cause."""
    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"username": "x-access-token", "password": "ghs_x"}).encode()

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        return _Response()

    monkeypatch.setattr(gc.urllib.request, "urlopen", fake_urlopen)
    gc.fetch_credential(
        "a/b",
        env={
            gc.CONTROL_URL_ENV: "https://control.example",
            gc.BOX_TOKEN_ENV: _token_for("b-from-token"),
            # Stale, and exactly what a machine keeps after an adoption.
            gc.BOX_ID_ENV: "b-stale-from-env",
        },
    )
    assert "b-from-token" in captured["url"]
    assert "b-stale-from-env" not in captured["url"]


def test_a_token_that_names_no_box_is_refused_before_the_network():
    """A malformed or non-box token has nothing to address. Better to say so
    than to build a URL out of an empty string and get a confusing 404."""
    with pytest.raises(gc.HelperError, match="does not name a box"):
        gc.fetch_credential(
            "a/b",
            env={gc.CONTROL_URL_ENV: "https://control.example", gc.BOX_TOKEN_ENV: "nonsense"},
        )
