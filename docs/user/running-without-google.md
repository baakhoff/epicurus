# Running without Google

epicurus is local-first: the assistant, your models, your memory, and most modules run
entirely on your box. Google is an **optional** account you connect to give a few modules
access to data that lives at Google — nothing more. This page is the short answer to "I don't
want to use the Google ones any more": what the two switches are, what stops working, and
what carries on exactly as before.

Nothing here is destructive, and nothing needs a restart.

## The two switches

There are two, at two different scopes, and they are independent:

| | Where | What it does | Reversible by |
| --- | --- | --- | --- |
| **Per module** | Modules → the module's card → its *Calendars* / *Lists* section | Stops **this one module** using Google. Every other module keeps working. Tokens are untouched. | **Use again**, one click |
| **Globally** | Settings → Connected accounts → **Disconnect** | Deletes the stored Google tokens for the whole platform. Every module loses access at once. | **Connect**, one consent flow |

Reach for the per-module switch when you want, say, local tasks but Google Calendar. Reach
for Disconnect when you want epicurus to hold no Google credentials at all.

## Going Google-free in one module

Open **Modules**, expand the module's card, and find the section named after what it manages
(*Calendars* for calendar, *Lists* for tasks). Under the Google account block:

> **Stop using Google in this module**

One click disables every one of that account's calendars/lists for this module. The write
target falls back to the module's **built-in local** store, and the panel says so — the block
collapses to a single quiet row:

> Google — not used · **Use again**

That is the whole state. It is stored as the module's ordinary collection selection (the same
thing the per-collection toggles write), so:

- **Your Google tokens are untouched.** Other modules — and the agent's Gmail tools — carry on.
  This is a *preference*, not a disconnection.
- **Nothing at Google is deleted.** Your calendars and task lists stay exactly as they are;
  epicurus simply stops reading and writing them here.
- **Your local data is already there.** The module has always had a local store; it now uses
  only that.

**Use again** re-enables all of the account's collections and makes the first writable one the
write target — the same seeding a fresh connect performs. If you had hand-picked a subset
before, that subset is not remembered (the module stores one selection, not a history): the
toggles show exactly what is on, so narrow it back down with a tick.

## Disconnecting Google entirely

**Settings → Connected accounts → Disconnect** deletes the stored access and refresh tokens
from the vault. Your OAuth *client credentials* (the client ID/secret you configured once) are
kept, so reconnecting later is one consent flow, not a re-setup.

On disconnect the core also strips Google from every module's stored collection selection, so
each module falls back to its local default on its own — you do not have to visit them.

### What stops working

- **Mail.** The mail module is Gmail-only: it has no local mailbox to fall back to. The Mail
  page shows an honest empty state ("Google is not connected — connect it in Settings, or
  disable the mail module"), and the agent's mail tools answer with the same sentence instead
  of an error. Nothing is lost; reconnecting brings the mailbox straight back, no restart.
- **Google calendars and Google task lists** disappear from the calendar and tasks pages, and
  from the agent's view of them.

If you don't intend to use Gmail again, turn the mail module off entirely: **Modules → mail →
the toggle**. It vanishes from the left nav and from the agent's tool list; the container keeps
running and the switch is reversible.

### What carries on

Everything that was never Google's to begin with:

- **Local calendar events** and **local tasks** — both modules keep their own store and stay
  fully usable, read *and* write.
- **Notes**, **knowledge** (your vault and its search), **storage** and the file space.
- **Chat, memory, automations, the model manager** — the whole core.
- **Web search**, and any other module that doesn't hold a Google connection.

The one thing to know: this is *not* a local replacement for the Google-backed capabilities
themselves. There is no local mailbox, and the local calendar is deliberately simpler than
Google's. Going Google-free means keeping the local half — not re-implementing the other one.

## "Couldn't check your mail connection"

If the Mail page says *"Couldn't check your mail connection"* rather than *"Google is not
connected"*, **don't reconnect anything** — the two are different messages on purpose. This one
means the mail module could not reach the core to look your account up, so it does not know
whether Google is connected; it prints the reason underneath. Your account is most likely fine.
Wait, or press **Try again**; if it persists, the core (or the network between them) is the thing
to look at, and `Modules → mail → Status` shows the same state and reason. Mail also stops
syncing in the background while this lasts, and says so once in the logs.

## Reconnecting

**Settings → Connected accounts → Connect.** One consent flow restores everything at once: the
mail page repopulates from its local cache immediately, and the calendar/tasks modules are
re-seeded with the account's collections (all enabled, the first writable one active). No
restart, no re-configuration.

## See also

- [OAuth 2.0 reference](../reference/oauth.md) — the connect/disconnect endpoints and what
  each one touches.
- [mail module](../services/mail.md) — why mail has no local provider, and what it does while
  disconnected.
- [web shell](../services/web.md) — the connected-accounts UI these switches live in.
