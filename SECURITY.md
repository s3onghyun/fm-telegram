# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's **Report a vulnerability** button (Security → Advisories) on this repository rather than in a public issue. Include the version or commit, what an attacker needs (for example: a Telegram account, local access) and what they gain. You should get a reply within a week.

Never include a real bot token, chat id or message content in a report; use fake values.

## Supported versions

Only the latest commit on `main` is supported.

## Scope

In scope: anything that lets a chat other than `owner_chat_id` create notes, read replies or trigger downloads; command or path injection from message content; token disclosure through logs, errors, files or outbound messages; files created with loose permissions.

Out of scope: Telegram's own platform security, a compromised local user account (it can already read your Keychain and Firstmate home), and content you deliberately send through the bot. See [docs/security-review.md](docs/security-review.md) for the threat model and known limits.
