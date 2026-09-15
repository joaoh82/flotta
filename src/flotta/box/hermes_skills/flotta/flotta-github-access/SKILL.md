---
name: flotta-github-access
description: "Which GitHub repos you can clone/push: run flotta-repos"
version: 1.0.0
author: Flotta
license: AGPL-3.0
metadata:
  hermes:
    tags: [GitHub, git, repositories, access, Flotta]
---

# GitHub access on a Flotta box

You run on a Flotta box. Your GitHub access is granted per repository by a
person, from the Flotta app. This skill is read-only and ships with the box
image; you cannot edit it.

## Which repositories can I use?

Run this, and nothing else:

```bash
flotta-repos
```

It asks the Flotta control plane with this box's own token and prints the
repositories you are granted. `flotta-repos --json` gives the same list as JSON.
The list is live: a grant or revocation made a moment ago is already reflected.

**Do not** try to work it out any other way. These all fail on a box, by
design, and cost the person waiting on you time:

- `flotta repo list`, `flotta ps` or any other `flotta` command. Those are the
  operator's tools. They need a fleet database and a signing key, and a box has
  neither.
- Looking for a GitHub token in the environment, `~/.config/gh`, or
  `git config`. There is none to find.
- Calling the GitHub API to probe access.

## Cloning, fetching and pushing

Use HTTPS URLs:

```bash
git clone https://github.com/<owner>/<name>.git
```

Credentials are supplied per operation by git's credential helper
(`credential.helper = flotta`). You never see or handle a token. `gh` works
the same way for granted repositories.

- **Granted:** clone, fetch, pull and push work normally.
- **Not granted:** git reports an authentication failure. The helper also
  prints a line starting with `flotta:` that names the repository and says it
  is not granted.
- **Public and not granted:** anonymous clone and fetch still work. Pushing does not.

## When the repository someone wants is not listed

You cannot grant yourself access. Tell the person plainly which repository is
missing, and that they can add it in the Flotta app: this agent's **Info**
panel, under **Repositories**. After they do, run `flotta-repos` again to
confirm, then continue. There is no need to restart anything.

## Commits

Commits are authored as this box (`<box-name>@<domain>`), already configured in
git. Do not change `user.name` or `user.email`.
