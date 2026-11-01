"""An agency's own GitHub: Flotta as a GitHub App (FLOTTA-22).

`$FLOTTA_GITHUB_TOKEN` is one person's token, so it stays the operator's. An
agency, or one of its clients, installs this App on its own GitHub account
instead, picks repositories on GitHub's screen, and the control plane trades the
App's private key for a token per request:

    the App's key -> a 10-minute JWT -> a 1-hour token for ONE repository

The box side does not change. It asks for a credential and gets one; only now
the credential cannot reach past the repository it was asked for, and GitHub
enforces that rather than Flotta. Removing the App on GitHub ends it, with
nothing for Flotta to do. Nothing long-lived is stored for anyone: an
installation id is not a secret, and the App's key is the operator's one secret.

## Why claiming an installation needs proof

**An `installation_id` is a number in a URL.** GitHub's redirect carries it and
GitHub's docs warn that it can be spoofed, so an agency that typed someone
else's id into its callback would get tokens for that account's repositories.
Three checks stand between that and a stored row:

- **The state says who started this**, signed here: which org, which scope, and
  which person. The callback must arrive in that person's session, so a link
  sent to somebody else claims nothing. Without that, an attacker could send a
  GitHub admin their own install link and collect the installation.
- **GitHub says who finished it.** The person authorizes the App, and the user
  token that yields is used for the check below and then dropped.
- **They must be an owner of the account**: that account itself, or an active
  admin of the organization. GitHub's own suggested check, whether the
  installation is "associated with that user", is not enough on a shared
  platform: it counts collaborators, so someone with read access to one
  repository could claim the owner's whole installation.

The store then allows an installation to one tenant at a time.

`fetch` is injected, so the suite never reaches GitHub.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

from flotta.github import API_ROOT

WEB_ROOT = "https://github.com"

SLUG_ENV = "FLOTTA_GITHUB_APP_SLUG"
CLIENT_ID_ENV = "FLOTTA_GITHUB_APP_CLIENT_ID"
CLIENT_SECRET_ENV = "FLOTTA_GITHUB_APP_CLIENT_SECRET"
PRIVATE_KEY_ENV = "FLOTTA_GITHUB_APP_PRIVATE_KEY"
REQUIRED_ENV = (SLUG_ENV, CLIENT_ID_ENV, CLIENT_SECRET_ENV, PRIVATE_KEY_ENV)

#: Long enough to pick repositories and approve; short enough that a state
#: found in a browser history is no use.
STATE_TTL_S = 30 * 60

#: GitHub allows ten minutes, and its clock may be behind ours.
_JWT_BACKDATE_S = 60
_JWT_TTL_S = 540

#: One page of a listing used only to suggest names. The grant form takes any
#: name, so one that is not on this page can still be typed.
_REPOSITORIES_PER_PAGE = 100

DEFAULT_TIMEOUT_S = 5.0

#: Separates a state's signature from a token's, which share the signing key.
_STATE_DOMAIN = b"flotta-github-state\0"

_API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


class GitHubAppError(Exception):
    """GitHub could not be asked, or answered in a way that decides nothing."""


class InstallationGone(GitHubAppError):
    """The App was removed from that account, or suspended on it."""


class RepositoryNotSelected(GitHubAppError):
    """The installation exists and that repository is not among those it covers."""


class NotProven(GitHubAppError):
    """GitHub did not confirm that this person may connect that account."""


class BadState(ValueError):
    """A state that is not ours, not finished, or too old."""


@dataclass(frozen=True, slots=True)
class Account:
    """The GitHub account an installation is on."""

    login: str
    #: GitHub's `User` or `Organization`.
    type: str
    suspended: bool = False


@dataclass(frozen=True, slots=True)
class State:
    """Who started connecting which scope, and once known, which installation."""

    org: str
    client: str | None
    subject: str
    installation: str | None = None


def sign_state(
    key: str,
    state: State,
    *,
    now: float | None = None,
    ttl_s: int = STATE_TTL_S,
) -> str:
    """A value GitHub carries through its redirect and hands back unchanged."""
    expires = int(time.time() if now is None else now) + ttl_s
    body = _b64(
        json.dumps(
            {
                "o": state.org,
                "c": state.client,
                "s": state.subject,
                "i": state.installation,
                "e": expires,
            },
            separators=(",", ":"),
        ).encode()
    )
    return f"{body}.{_state_signature(key, body)}"


def read_state(key: str, raw: str, *, now: float | None = None) -> State:
    """The state, or `BadState` for one we did not sign or that has expired."""
    body, dot, signature = (raw or "").partition(".")
    if not dot or not hmac.compare_digest(signature, _state_signature(key, body)):
        raise BadState("that link did not come from this Flotta; start connecting again")
    try:
        fields = json.loads(_unb64(body))
    except (ValueError, binascii.Error) as exc:
        raise BadState("that link is not readable; start connecting again") from exc
    if int(fields["e"]) < (time.time() if now is None else now):
        raise BadState("that link has expired; start connecting again")
    return State(
        org=str(fields["o"]),
        client=fields["c"],
        subject=str(fields["s"]),
        installation=fields["i"],
    )


def _state_signature(key: str, body: str) -> str:
    return _b64(hmac.new(key.encode(), _STATE_DOMAIN + body.encode(), hashlib.sha256).digest())


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _httpx_fetch(method: str, url: str, **kwargs: Any) -> tuple[int, Any]:
    """The status and the parsed body. Lazy: the CLI imports the control plane."""
    import httpx

    response = httpx.request(method, url, **kwargs)
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {}


@dataclass(frozen=True, slots=True)
class GitHubApp:
    """Everything Flotta asks of GitHub as the App, or as a person authorizing it."""

    slug: str
    client_id: str
    client_secret: str = field(repr=False)
    private_key: str = field(repr=False)
    fetch: Callable[..., tuple[int, Any]] = _httpx_fetch
    api_root: str = API_ROOT
    web_root: str = WEB_ROOT
    now: Callable[[], float] = time.time

    # -- where a person goes ---------------------------------------------------

    def install_url(self, state: str) -> str:
        """GitHub's own screen: which account, which repositories."""
        return f"{self.web_root}/apps/{quote(self.slug)}/installations/new?state={quote(state)}"

    def authorize_url(self, state: str) -> str:
        """GitHub asks who this is, and comes back with a code that proves it."""
        query = urlencode({"client_id": self.client_id, "state": state})
        return f"{self.web_root}/login/oauth/authorize?{query}"

    # -- proving who is connecting ---------------------------------------------

    def proven_account(self, installation: str, code: str) -> Account:
        """The account `installation` is on, if the person behind `code` owns it.

        The user token ends with this call: it is never stored or returned.
        """
        account = self.account(installation)
        user_token = self._user_token(code)
        if not self._is_owner(user_token, account):
            raise NotProven(
                f"GitHub does not say you are an owner of {account.login}. Only an owner "
                f"of a GitHub account can connect it here."
            )
        return account

    def _user_token(self, code: str) -> str:
        # GitHub answers 200 with an `error` field for a bad code.
        status, body = self._ask(
            "POST",
            f"{self.web_root}/login/oauth/access_token",
            headers={"Accept": "application/json"},
            json={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
            },
        )
        token = body.get("access_token") if isinstance(body, dict) else None
        if status != 200 or not token:
            raise NotProven("GitHub did not confirm who you are; start connecting again")
        return str(token)

    def _is_owner(self, user_token: str, account: Account) -> bool:
        headers = {**_API_HEADERS, "Authorization": f"Bearer {user_token}"}
        if account.type == "User":
            status, body = self._ask("GET", f"{self.api_root}/user", headers=headers)
            if status != 200:
                raise NotProven("GitHub did not confirm who you are; start connecting again")
            return str(body.get("login") or "").lower() == account.login.lower()
        if account.type != "Organization":
            return False
        status, body = self._ask(
            "GET",
            f"{self.api_root}/user/memberships/orgs/{quote(account.login)}",
            headers=headers,
        )
        # 403 and 404 both mean GitHub will not say they are a member.
        return status == 200 and body.get("state") == "active" and body.get("role") == "admin"

    # -- as the App ------------------------------------------------------------

    def account(self, installation: str) -> Account:
        """The account an installation is on, or `InstallationGone`."""
        status, body = self._as_app("GET", f"/app/installations/{quote(installation)}")
        if status == 404:
            raise InstallationGone(f"GitHub has no installation {installation}")
        if status != 200:
            raise GitHubAppError(f"GitHub answered {status} about installation {installation}")
        owner = body.get("account") or {}
        return Account(
            login=str(owner.get("login") or ""),
            type=str(owner.get("type") or ""),
            suspended=bool(body.get("suspended_at")),
        )

    def token_for(self, installation: str, repository: str) -> str:
        """A 1-hour token good for one repository, by its name without the owner."""
        return self._installation_token(installation, {"repositories": [repository]})

    def repositories(self, installation: str) -> tuple[list[str], bool]:
        """`owner/name` for what it covers, and whether there are more than these.

        The token only reads this list and is then dropped, so it can do nothing else.
        """
        token = self._installation_token(installation, {"permissions": {"metadata": "read"}})
        status, body = self._ask(
            "GET",
            f"{self.api_root}/installation/repositories?per_page={_REPOSITORIES_PER_PAGE}",
            headers={**_API_HEADERS, "Authorization": f"Bearer {token}"},
        )
        if status != 200:
            raise GitHubAppError(f"GitHub answered {status} listing repositories")
        names = [str(r["full_name"]).lower() for r in body.get("repositories") or []]
        return names, int(body.get("total_count") or 0) > len(names)

    def installation_of(self, repository: str) -> str | None:
        """Which installation of this App covers `owner/name`, if any."""
        status, body = self._as_app("GET", f"/repos/{repository}/installation")
        if status == 404:
            return None
        if status != 200:
            raise GitHubAppError(f"GitHub answered {status} about {repository}")
        return str(body.get("id"))

    def uninstall(self, installation: str) -> None:
        """Take the App off that account. Already gone is the same outcome."""
        status, _ = self._as_app("DELETE", f"/app/installations/{quote(installation)}")
        if status not in (202, 204, 404):
            raise GitHubAppError(f"GitHub answered {status} removing installation {installation}")

    def _installation_token(self, installation: str, body: dict[str, Any]) -> str:
        status, reply = self._as_app(
            "POST", f"/app/installations/{quote(installation)}/access_tokens", json=body
        )
        if status == 201 and reply.get("token"):
            return str(reply["token"])
        if status == 404:
            raise InstallationGone(f"GitHub has no installation {installation}")
        if status == 403:
            raise InstallationGone(f"installation {installation} is suspended on GitHub")
        if status == 422:
            raise RepositoryNotSelected(f"installation {installation} does not cover that")
        raise GitHubAppError(f"GitHub answered {status} asked for a token")

    def _as_app(self, method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
        headers = {**_API_HEADERS, "Authorization": f"Bearer {self.jwt()}"}
        return self._ask(method, f"{self.api_root}{path}", headers=headers, **kwargs)

    def jwt(self) -> str:
        """The App signing as itself: RS256, with the client id as issuer."""
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        moment = int(self.now())
        header = {"alg": "RS256", "typ": "JWT"}
        claims = {
            "iat": moment - _JWT_BACKDATE_S,
            "exp": moment + _JWT_TTL_S,
            "iss": self.client_id,
        }
        signing_input = ".".join(
            _b64(json.dumps(part, separators=(",", ":")).encode()) for part in (header, claims)
        )
        try:
            key = serialization.load_pem_private_key(self.private_key.encode(), password=None)
        except ValueError:
            # Not the exception's text: nothing near a key gets printed.
            raise GitHubAppError(f"${PRIVATE_KEY_ENV} is not a readable PEM key") from None
        if not isinstance(key, rsa.RSAPrivateKey):
            raise GitHubAppError(f"${PRIVATE_KEY_ENV} is not an RSA key")
        signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{signing_input}.{_b64(signature)}"

    def _ask(self, method: str, url: str, **kwargs: Any) -> tuple[int, Any]:
        try:
            status, body = self.fetch(method, url, timeout=DEFAULT_TIMEOUT_S, **kwargs)
        except Exception as exc:  # noqa: BLE001 - any transport failure is "could not ask"
            # The type alone: a transport error can quote the request it failed on.
            raise GitHubAppError(f"could not reach GitHub ({type(exc).__name__})") from None
        return status, body if isinstance(body, dict) else {}


def missing_settings(env: Mapping[str, str]) -> list[str]:
    """The variables this deployment still needs for the App: names only."""
    return [name for name in REQUIRED_ENV if not (env.get(name) or "").strip()]


def app_from_env(env: Mapping[str, str]) -> GitHubApp | None:
    """The App this deployment is registered as, or `None` if it is not."""
    if missing_settings(env):
        return None
    return GitHubApp(
        slug=env[SLUG_ENV].strip(),
        client_id=env[CLIENT_ID_ENV].strip(),
        client_secret=env[CLIENT_SECRET_ENV].strip(),
        # A hosting dashboard may keep a PEM's line breaks as the two characters.
        private_key=env[PRIVATE_KEY_ENV].strip().replace("\\n", "\n"),
    )
