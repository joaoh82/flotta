"""A GitHub token for one repository one box is granted: the one rule, kept in one place.

The box's credential helper asks for one (`POST /api/boxes/{id}/git-credential`),
and since FLOTTA-171 the control plane asks for one itself, to read and merge
the pull requests a box opened (`flotta.pulls`). Both must refuse exactly the
same things, so both call `repo_token`: a repository the box is not granted, a
grant whose GitHub connection has gone, and the fleet's own token anywhere but
the operator's agents. If the two disagreed, the agency could merge from Flotta
what its agent could not push, or the other way round.

The refusals carry an HTTP status and a sentence for a person, because the
route answers with them as they are; the sweep keeps the sentence as the pull
request's `check_error`.
"""

from __future__ import annotations

from typing import Any

from flotta.github import GITHUB_TOKEN_ENV


class RepoTokenError(Exception):
    """Why there is no token, as the HTTP status and words the route answers with."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def repo_token(
    store: Any, box: Any, repo: str, *, github_app: Any, fleet_token: str
) -> tuple[str, str, str]:
    """A token for `repo` on `box`'s behalf: `(token, source, repo)`, where
    `repo` is the name normalised and `source` is `github_app` or `fleet_token`.

    `github_app` is the deployment's App, or None when it has none; `fleet_token`
    is `$FLOTTA_GITHUB_TOKEN`, or empty. Raises `RepoTokenError`.
    """
    from flotta.github_app import GitHubAppError, InstallationGone, RepositoryNotSelected
    from flotta.store import AGENCY_GITHUB_UNAVAILABLE, normalise_repo

    try:
        if not store.may_use_repo(box.id, repo):
            raise RepoTokenError(
                403,
                f"box {box.name!r} is not granted {repo!r}. Grant it on the "
                f"agent's page, or with `flotta repo grant {box.name} {repo}`.",
            )
        repo = normalise_repo(repo)
    except ValueError as exc:
        raise RepoTokenError(422, str(exc)) from exc

    installation = store.github_installation_for(box.org, box.client, repo)
    if installation is None:
        # Whatever the grants say: a grant from before FLOTTA-83 must not hand
        # another org's agent the operator's token, and one made on an account
        # since disconnected was never authorised to use it.
        if not store.may_spend_fleet_token(box.id, repo):
            raise RepoTokenError(403, AGENCY_GITHUB_UNAVAILABLE)
        if not fleet_token:
            raise RepoTokenError(
                503,
                f"no ${GITHUB_TOKEN_ENV} configured on the control plane; "
                f"boxes can read public repositories and nothing else",
            )
        return fleet_token, "fleet_token", repo

    if github_app is None:
        import os

        from flotta.github_app import missing_settings

        raise RepoTokenError(
            503,
            "this deployment has no GitHub App; it still needs "
            + ", ".join(f"${name}" for name in missing_settings(os.environ)),
        )
    try:
        token = github_app.token_for(installation.installation_id, repo.split("/")[1])
    except InstallationGone as exc:
        raise RepoTokenError(
            403,
            f"the GitHub connection to {installation.account} was removed "
            f"or suspended on GitHub. Connect it again to use {repo}.",
        ) from exc
    except RepositoryNotSelected as exc:
        raise RepoTokenError(
            403,
            f"{repo} is no longer among the repositories selected for "
            f"Flotta on {installation.account}. Add it there on GitHub.",
        ) from exc
    except GitHubAppError as exc:
        raise RepoTokenError(502, str(exc)) from exc
    return token, "github_app", repo
