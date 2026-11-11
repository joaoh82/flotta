---
name: flotta-screen
description: "A person for sign-in, 2FA, CAPTCHA: flotta-handoff"
version: 1.0.0
author: Flotta
license: AGPL-3.0
metadata:
  hermes:
    tags: [screen, browser, sign-in, handoff, agency, Flotta]
    # Only an agent with a screen is offered this: one without has the browser
    # toolset turned off at boot (`flotta.box.screen`), and the command refuses.
    requires_toolsets: [browser]
---

# Handing your screen to a person

You have a screen: a desktop with a browser, which the agency's people can
watch and take over. Some things on it must be done by a person, not by you.
This skill says when, and how to ask. It is read-only and ships with the box
image; you cannot edit it.

## When to hand over

Hand the screen to a person when the page in front of you needs:

- **a sign-in**: a username and password, or "Sign in with" a provider
- **a 2FA or one-time code**: an authenticator, an SMS, an email code, a passkey
- **a CAPTCHA** or any "prove you are human" check
- **a payment**: a card, a bank, a checkout confirmation
- **a site that requires a human** in any other way: an identity check, a
  consent only the account's owner may give, a security question

Do not try to get around any of these, and do not guess.

## Never ask for a password or a code in chat

**Never ask anyone for a password, a one-time code, a recovery code, a card
number or any other secret in the conversation**, and never type one you were
sent there. The person types it on the screen, where it goes to the site and
nowhere else. If someone pastes one into the conversation anyway, do not use
it: tell them to enter it on the screen instead.

## How

1. Leave the page where the person needs to act open on the screen.
2. Run:

   ```bash
   flotta-handoff "Sign in to the client's Shopify admin" --url "https://admin.shopify.com/"
   ```

   The first argument says what the person is to do, in a line. `--url` is
   the page, if there is one. The agency is told at once: it is on their Home
   with **Open screen**, and they are emailed.
3. **End your turn.** Say in your reply what you need the person to do on the
   screen, and that they should hand it back when they are done. Do not wait,
   poll or retry the page while you wait: while a person holds the screen your
   screen and browser tools are refused anyway.

When they hand the screen back you are told to carry on. In a run, that
happens in the same conversation, by itself. Check the page first: they may
have signed in, or they may not have managed to.

If `flotta-handoff` fails, the agency was not told. End your turn anyway, and
say in your reply what you need a person to do on your screen.

## What a sign-in leaves behind

Whatever a person signs in to on your screen stays signed in, in your browser
profile, after they hand it back. That is what lets you carry on. Use it only
for the work you were given, and never copy a session, a cookie or a token out
of the browser.
