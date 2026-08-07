# V5.1 admin detection fix

- Removed `messages.GetAdminsWithInvitesRequest` as the admin-access probe.
- Channel scanning now uses Telegram's `Channel.creator` / `Channel.admin_rights` flags.
- Registration refreshes the channel entity and checks the `invite_users` admin right.
- Invite enumeration no longer depends on `GetAdminsWithInvitesRequest`.
- It fetches the connected account's invite links first, then enumerates admins with `ChannelParticipantsAdmins` and fetches their links individually.
- If Telegram refuses secondary admin enumeration, the dashboard still remains usable for links created by the connected account.
