---
name: flotta-requests
description: "File work someone asks for before starting: flotta-request"
version: 1.0.0
author: Flotta
license: AGPL-3.0
metadata:
  hermes:
    tags: [requests, approval, agency, clients, Flotta]
---

# Requests on a Flotta box

You work for an agency, usually for one of its clients. The agency decides how
big a piece of work you may take on by yourself. Anything bigger waits for
them to say yes, because it needs planning and design with people first. This
skill is read-only and ships with the box image; you cannot edit it.

## When to file one

When someone in a conversation asks you **to do a piece of work**, file it
before you start:

```bash
flotta-request "Add a CSV export to the orders page" --details "What they asked, in their words, and anything you know about it"
```

A piece of work is anything that changes something or produces something: a
feature, a fix, a change, a migration, a report, an investigation. Use
`--details -` to pass longer details on stdin.

**Do not** file one for questions you can answer, for explaining, for
small talk, or for the next step of work that was already approved. One
request per piece of work, not one per message.

## What happens

`flotta-request` answers at once, in one of two ways. Do what it says.

- **Approved**: it is within what you may start on your own. Go ahead in the
  same conversation.
- **Held for the agency**: do not start it. Tell the person, plainly, that you
  have passed it to the agency, which decides on work like this before it
  starts, and that it will be picked up once they approve it. Keep helping with
  anything else. If they approve it, you are sent it as a piece of work of its
  own, with any note they added.

If `flotta-request` fails, the request was not filed. Say so, and do not
start work that needs planning.

## Checking on them

```bash
flotta-request list
```

shows every request you filed and how the agency answered, with its note. When
someone asks how their request is going, look here rather than guessing.
