---
name: flotta-projects
description: "Where to clone: run flotta-workdir"
version: 1.0.0
author: Flotta
license: AGPL-3.0
metadata:
  hermes:
    tags: [git, projects, clone, workspace, Flotta]
---

# Projects on a Flotta box

You run on a Flotta box. Your **projects folder** is where you clone
repositories and do engineering work. This skill is read-only and ships with
the box image; you cannot edit it.

## Where is it?

```bash
flotta-workdir
```

It prints the path (also `$FLOTTA_WORKDIR`, default `/workspace`) and whether
the folder survives an update. `flotta-workdir --json` for the same facts as
JSON.

**Do not** clone into `/data` or `$HERMES_HOME`. That volume is ~1 GB and holds
your memory. A `node_modules` there fills it and takes the memories with it.

## Before starting work on a repository

1. **Check whether it is already cloned** in the projects folder.
2. **If it is:** `cd` into it, `git fetch`, and sync `main` (or `master`)
   before you start. Then do the work on a branch.
3. **If it is not:** clone it there, then the same.

Do this without being asked. "Review this PR" means: make sure the repo is in
the projects folder, on an up-to-date `main`, then review.

## Updates wipe this folder

An image update (`Update agents` in the Flotta app) replaces the machine's
disk. Everything in the projects folder is gone: clones, uncommitted edits,
`node_modules`. Your memory, skills and conversation history live on `/data`
and **are** kept.

If the folder is empty after an update, re-clone. Do not tell the person the
project vanished as if it were a bug, and do not ask them to clone it for you.

## Cloning

Use HTTPS URLs, the same way the GitHub-access skill describes:

```bash
git clone https://github.com/<owner>/<name>.git
```

Clone into the projects folder, not into `/data`, `/root`, or the current
working directory if that is not the projects folder. `flotta-workdir` is the
authority on which path that is.
