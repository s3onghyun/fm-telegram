# Security review

Reviewed: 2026-10-17, before the first public release. Scope: `fm_telegram.py`, `oss_watch.py`, `install.sh`, `launchd/`, the CI workflow and the git history.

## Threat model

| Actor | Can do | Must not be able to |
|-------|--------|---------------------|
| Any Telegram user who finds the bot | Send it messages | Create notes, trigger downloads, read replies or status, learn anything but "no answer" |
| The owner's chat (`owner_chat_id`) | Create notes, send attachments, read replies and `/status` | Run commands, write outside `media/`, read the token |
| Telegram (the API endpoint, over TLS) | Deliver updates and files | Not modelled as an attacker; its responses are still parsed defensively |
| Other local users on the machine | Read world-readable files | Read state, config, attachments or logs; change which program the bridge runs |
| The local user account itself | Everything | Out of scope (it can already read the Keychain and the Firstmate home) |

## Findings and fixes

| # | Area | Finding | Severity | Status |
|---|------|---------|----------|--------|
| 1 | Configuration | The code shipped the original owner's chat id as the default and wrote it into a new config automatically. A fresh install by anyone else would have accepted that person's messages and sent them receipts, `/status` and replies. Firstmate home and denylist paths were also hard-coded to one machine. | High (for a public release) | **Fixed.** No defaults: `owner_chat_id` is required and must be an integer; `firstmate_home` is required (or `FM_HOME`). Config moved to `~/.config/fm-telegram/config.json`; the bridge never writes it. |
| 2 | Error handling | Telegram reports rejected requests as HTTP 4xx, which `urlopen` raises as `HTTPError`. It was caught as a generic network error, so the plain-text fallback for rejected HTML never ran; a reply that Telegram refused stayed un-acknowledged and was retried and rejected on every poll. | Medium | **Fixed.** `HTTPError` becomes `TelegramAPIError("… (HTTP <code>)")` raised `from None` (no chained exception holding the token URL) and the response is closed. Covered by unit and end-to-end tests. |
| 3 | Outbound redaction | The bridge did not redact its own bot token or Telegram-format tokens, so a reply quoting a config or log could echo the token into the chat. | Medium | **Fixed.** The literal token is always redacted, and patterns were added for Telegram bot tokens, `gho_/ghs_/ghu_/ghr_`, `github_pat_` and PEM private keys, alongside the existing GitLab, GitHub, OpenAI-style, Slack, AWS and `Bearer` patterns. The same redactor wraps every log record. |
| 4 | Configuration integrity | The config chooses `firstmate_home`, and the bridge executes `<firstmate_home>/bin/fm-inbox.sh`. A config writable by another user would be a code-execution path. | Medium (hardening) | **Fixed.** The bridge refuses a config that is not owned by the current user or is group/world writable, with a `chmod 600` hint. |
| 5 | Transport | `api_base` is now configurable (for the fake test server). Pointing it at plain HTTP would send the token in cleartext. | Medium (new surface) | **Fixed.** Only `https://` is accepted, except `http://` to `localhost`, `127.0.0.1` or `::1`. Certificate verification is Python's default. |
| 6 | Replay handling | `fm-inbox.sh` marks a request-id replay as `"outcome": "replay"`, which the bridge did not recognise, so a redelivered update re-rang the doorbell. | Low | **Fixed** and tested against a scripted `fm-inbox.sh`. |
| 7 | File permissions | `state.json` was written with the process umask and only then `chmod`ed to 0600, leaving a short window where it could be world-readable. | Low | **Fixed.** Temporary files are created with `os.open(…, 0o600)` and `O_NOFOLLOW`, then atomically renamed. |
| 8 | Attachment paths | Names were already reduced to the last path component and stripped of separators; the write followed symlinks. | Low | **Hardened.** Writes use `O_NOFOLLOW`. A test sends `../../etc/<name>` and checks that the file lands in `media/`. Generated names are `<int update id>-<[A-Za-z0-9_-]{1,64} unique id>[-<≤50 word chars>].<ext>`. |
| 9 | Terminal input | The doorbell types a line into an agent's terminal. The note id in it comes from `fm-inbox.sh`, not from Telegram, but was not validated. | Low (hardening) | **Fixed.** Ids must match `[A-Za-z0-9._:-]{1,80}`; anything else is logged and skipped. Message text is never typed. |
| 10 | Output size | `notify` and replies had no size bound; a large input became hundreds of messages. | Low | **Fixed.** Outbound text is capped at 20,480 characters (five Telegram messages) and marked `… (truncated)`. |
| 11 | LaunchAgents | Plists ran `/usr/bin/env python3` (resolved through `PATH` at every start), had machine-specific paths, and logs were created with the default umask. | Low | **Fixed.** Plists are templates; `install.sh` writes the absolute `python3` path, sets `Umask` 077, adds `ThrottleInterval` 30 to the KeepAlive bridge, and installs per-user agents in the `gui/<uid>` domain only (no `UserName`, no root, no LaunchDaemon). |
| 12 | Owner data in history | Git history contains the original owner's chat id (29 occurrences) and absolute home paths (19). `gitleaks git` over all 9 commits found no secrets. | Info | **Mitigated.** The public release is a fresh single-commit history (`public` branch) built from the cleaned tree. |
| 13 | CI | New workflow. | Info | `permissions: contents: read`, `actions/checkout` pinned by commit SHA with `persist-credentials: false`, no secrets, no third-party actions. |

## Verified without changes

| Area | Result |
|------|--------|
| Token storage | Read at start-up from `FM_TELEGRAM_BOT_TOKEN` or the login Keychain (`security find-generic-password … -w`; the token arrives on stdout, never in argv). Never stored in state, config or logs. The README's `security add-generic-password … -w` form prompts for the token so it stays out of shell history. Errors interpolate only fixed text. |
| Allowed-chat check | The first check after the update id. A non-matching chat returns before any download, note, reply or command; only its integer id is logged. Only `message` updates are requested, so edits and channel posts never arrive. |
| Command execution | No `shell=True` anywhere. Every subprocess gets an argument list. Message text reaches `fm-inbox.sh` on stdin; the only Telegram-derived argument is `tg:<integer update id>`. `afconvert` and `whisper-cli` receive only paths the bridge generated, plus configured binary/model/language values, with 60 s and 180 s timeouts. `oss_watch.py` passes only configured repository names to `gh`. |
| Input size | Telegram bounds text (4096) and captions (1024). Attachments are refused above 20 MB using the declared size before download and again while reading (`read(limit + 1)`). |
| Logging | Logs contain chat ids of ignored chats, note ids and exception type names. Message text, captions, transcripts and file contents are never logged. |
| Update offset | The offset is saved after each update is handled. A crash in between redelivers the update, and the request id `tg:<update_id>` makes `fm-inbox.sh` return the existing note (verified end to end). Reply delivery advances its cursor only after the send succeeds. |

## Residual risks and limits

- **Telegram is not end-to-end encrypted** for bots, and messages are stored on Telegram's servers. The README tells users not to send secrets, customer data, logs or diffs.
- **Group chats:** if `owner_chat_id` is a group, every member can create notes. The README tells users to use their private chat; the sender id is not checked separately.
- **Head-of-line blocking:** if `fm-inbox.sh` keeps failing, the same update is retried with backoff (up to 60 s) and later updates wait behind it. Nothing is lost unless the outage exceeds Telegram's roughly 24-hour retention of undelivered updates.
- **Media decoders:** `afconvert` and whisper.cpp parse attacker-shaped media, but only media from the allowed chat.
- **Disk use:** `media/` is never pruned; attachments accumulate until removed by hand.
- **Environment tokens** are visible to other processes of the same user. Prefer the Keychain on macOS.
- **Redaction** is pattern- and denylist-based and will miss secrets in unknown formats.
