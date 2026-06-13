# epicurus-mail

Mail module for the epicurus platform.  Gives the agent the ability to search,
read, and send mail via a pluggable provider — Gmail is the v0.1 provider.
`mail_send` is a guarded danger action that requires explicit user confirmation.

See the full documentation page at **[docs/services/mail.md](../../docs/services/mail.md)**.

## Wiring checklist

When this module is added to the stack:

1. Fragment included in root `compose.yaml` `include:` list ✓
2. `http://mail:8080` registered in `module_urls` in core-app settings ✓
3. Port `8087` assigned (unique across all modules) ✓
4. Connect Google with Gmail scopes before using mail tools (see [docs](../../docs/services/mail.md#connect-your-google-account))
