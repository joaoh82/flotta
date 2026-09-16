---
name: flotta-colleagues
description: "Ask another agent on this fleet: run flotta-ask"
version: 1.0.0
author: Flotta
license: AGPL-3.0
metadata:
  hermes:
    tags: [agents, delegation, colleagues, teamwork, Flotta]
---

# Talking to other agents on this fleet

You run on a Flotta box, and you are not the only agent here. Other agents have
their own machines, their own memory and their own specialities. You can ask
one of them a question and get their answer back. This skill is read-only and
ships with the box image; you cannot edit it.

## Who can I ask?

```bash
flotta-ask --list
```

It prints the agents you may message, with what each one is for. The list is
live: an agent created a moment ago is already there. `--json` for the same list as
JSON.

## Asking

```bash
flotta-ask eng-b "Does the front door wake a stopped box, or does the caller?"
```

The command blocks while they think — often thirty seconds, sometimes a couple
of minutes, because it wakes their machine if it is asleep. That is normal. It
prints their reply and exits.

**Send everything they need in the one message.** They cannot see your
conversation, they do not know what you are working on, and they get no chance
to ask you a follow-up. A question with the context in it gets a useful answer;
"what do you think?" does not.

## When to ask, and when not to

Ask when a colleague genuinely knows something you do not — their speciality,
their repositories, work they did. Their answer costs a machine waking up and a
model call, and the person is waiting on you meanwhile.

Do not ask a colleague to do something you can do yourself, and do not relay a
question you were asked without adding what you already know. If you were asked
to check with someone, do it once and report what they said.

## When the person names an agent

A person may write `@eng-g` in their message. That is another agent on this
fleet. If they suggest it can help, ask it with `flotta-ask eng-g "…"` —
putting everything it needs in the question — and tell the person what it
said. The name after `@` is exactly what you type after `flotta-ask`.

## Answering a colleague

A message that starts with another agent's name means one of them is asking
you. Answer it directly, in that conversation. They see your reply and nothing
else, so make it complete and do not ask them a question back — they cannot
answer.

## Limits, so you are not surprised

- **You can ask any agent on this fleet** unless a person has stopped you
  asking that one. `flotta-ask --list` shows who you can ask right now. If the
  agent you need is not there, say so to the person you are working with —
  they can change it in the Flotta app, under this agent's **Info** panel,
  **Colleagues**. You cannot change it yourself.
- **An agent that is still being set up cannot answer yet.** Try again in a
  few minutes, or answer without them.
- **A question can only be passed along so far.** If you were asked something
  by another agent and you ask a third, that is as far as it goes. When you are
  refused for this reason, answer with what you have rather than finding
  another route.
- **One at a time.** An agent already answering somebody else refuses politely;
  try again later or answer without them.

Every refusal says what to do instead. Read it and follow it rather than trying
the same thing a different way.
