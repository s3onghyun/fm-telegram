# fm-telegram

A private Telegram bot bridge for [Firstmate](https://github.com/kunchenguid/firstmate). Message your bot from your phone and each message becomes a durable Firstmate inbox note; when Firstmate replies to that note, the reply comes back to the same Telegram chat.

An out-of-tree bridge built only on `fm-inbox.sh` note, receipts and ready, it complements Firstmate's [pluggable out-of-band notifier proposal](https://github.com/kunchenguid/firstmate/issues/106) and [in-tree Telegram bridge](https://github.com/kunchenguid/firstmate/pull/4364).

- One Python file, standard library only (Python 3.8+).
- Long polling (`getUpdates`) only: no webhook, no open port, no public endpoint.
- Exactly one allowed chat. Everything from any other chat is ignored before it is parsed.
- Text, photos, files, voice notes and videos. Voice is transcribed **on your machine** with whisper.cpp.
- Outbound text is redacted (tokens, keys, your own denylist) and rendered as safe Telegram HTML.

## How it works

```text
phone ──► Telegram Bot API ◄── getUpdates (HTTPS, outbound only) ── fm_telegram.py
                                                                     │
                       fm-inbox.sh note --request-id tg:<update id> ◄┘  (message on stdin)
                       fm-inbox.sh receipts  ──► replies to Telegram notes ──► sendMessage
                       herdr pane send-text  ──► optional "doorbell": a fixed line with the note id
```

Each Telegram update becomes `fm-inbox.sh note --request-id tg:<update_id>`, so a redelivered update returns the same note instead of creating a second one. The bridge remembers which notes came from Telegram and forwards only replies to those notes. The optional doorbell wakes the Firstmate primary pane through Herdr by typing a fixed line containing only the note id; message text never reaches a terminal.

## Requirements

| What | Why |
|------|-----|
| A Firstmate home with `bin/fm-inbox.sh` | Notes and replies go through Firstmate's inbox. |
| Python 3.8 or newer | Runs the bridge. No packages to install. |
| A Telegram bot token from [@BotFather](https://t.me/BotFather) | The bot you message. |
| macOS Keychain, or the `FM_TELEGRAM_BOT_TOKEN` environment variable | Where the token is read from. |
| Optional: `herdr` on `PATH` | The doorbell. Without it, notes still land in the inbox. |
| Optional: macOS `afconvert` + `whisper-cli` + a model | Voice transcription (see below). |

macOS is the primary platform (Keychain, LaunchAgents, `afconvert`). The bridge itself also runs on Linux with the token in `FM_TELEGRAM_BOT_TOKEN`; voice notes are then delivered as attachments with a "transcription failed" line.

## Setup

On Linux, skip the `security` command in step 2 and export `FM_TELEGRAM_BOT_TOKEN` instead, and in step 6 run the bridge in a terminal, tmux or a systemd user service: `./install.sh --agent` is macOS-only. To try the bridge without Telegram first, see [Try it without Telegram](#try-it-without-telegram).

1. **Create a bot.** In Telegram, talk to [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token. Send any message to your new bot so it has something to deliver.

2. **Store the token.** On macOS, put it in the login Keychain. With `-w` last and no value, `security` prompts for the token, so it never lands in your shell history:

   ```sh
   security add-generic-password -U -s firstmate-telegram-bot -a "$USER" -w
   ```

   Elsewhere, export `FM_TELEGRAM_BOT_TOKEN` in the environment that starts the bridge.

3. **Install.** This copies `fm_telegram.py` and `oss_watch.py` to `~/.local/share/fm-telegram/` (mode 0700). It does not load anything.

   ```sh
   git clone https://github.com/s3onghyun/fm-telegram.git
   cd fm-telegram
   ./install.sh
   ```

4. **Write the config.** `owner_chat_id` is the one chat allowed to talk to the bridge; use `0` until you know it. `firstmate_home` is your Firstmate checkout.

   ```sh
   mkdir -p ~/.config/fm-telegram && chmod 700 ~/.config/fm-telegram
   (umask 077 && cat > ~/.config/fm-telegram/config.json) <<'JSON'
   {
     "owner_chat_id": 0,
     "firstmate_home": "~/firstmate"
   }
   JSON
   ```

5. **Find your chat id.** Run the bridge once. Every message from a chat that is not allowed is logged with its numeric id, so send your bot a message and read the id from the log:

   ```sh
   python3 ~/.local/share/fm-telegram/fm_telegram.py run
   # ... WARNING ignored Telegram message from chat id=123456789
   ```

   Stop it with Ctrl-C, put that number in `owner_chat_id`, and start it again. Use your **private** chat with the bot: in a group, every member could create notes. The message you sent to find the id does not become a note; send a new one after restarting.

6. **Keep it running.** Run it in a terminal, tmux or a Herdr pane:

   ```sh
   python3 ~/.local/share/fm-telegram/fm_telegram.py run
   ```

   Or, on macOS only, load it as a per-user LaunchAgent (logs go to `~/Library/Logs/fm-telegram.log`). Without `launchctl`, `install.sh --agent` stops with an error before installing anything:

   ```sh
   ./install.sh --agent bridge
   ```

   macOS privacy protection stops LaunchAgents from reading `~/Documents`, `~/Desktop` and iCloud Drive. If your Firstmate home lives there, run the bridge in a terminal instead of granting Python Full Disk Access.

   To remove an agent: `launchctl bootout "gui/$(id -u)/io.github.fm-telegram.fm-telegram"` and delete its plist from `~/Library/LaunchAgents/`.

## Using it

From your phone, send the bot:

- **Text** — becomes a note. The bot answers `Received. Note ID: <id>`.
- **Photos and files** (up to 20 MB, the Bot API download limit) — saved to the state directory's `media/` folder; the note contains the caption (or `(photo)` / `(file)`) and an `attachment: <absolute path>` line. File names are kept but reduced to the last path component with separators and punctuation replaced by `_`.
- **Voice, audio, video, video notes** — saved the same way and transcribed locally. The note starts with the transcript, then the caption, `(voice)` and the `attachment:` line.
- **Anything else** (stickers, locations, files over 20 MB) — never dropped silently: a note records what arrived, and the bot tells you it could not be saved.
- `/status` — a short Firstmate inbox status. `/help` — usage.

Firstmate answers with `fm-inbox.sh reply <note id> <text>`; the bridge picks the reply up from `fm-inbox.sh receipts` and sends it to you. Firstmate can also push a notification at any time:

```sh
python3 ~/.local/share/fm-telegram/fm_telegram.py notify 'Deploy finished'
echo 'longer text' | python3 ~/.local/share/fm-telegram/fm_telegram.py notify -
```

### Decision buttons

For a notification that needs a structured answer, add one `--choice QUESTION:OPTION=LABEL`
per button. Choices with the same question identifier are one Telegram row; use the same
identifier at the start of the matching prompt line so the reply note can include that question.

```sh
printf '1) Open the deployment now?\n2) Run the migration?\n' | \
python3 ~/.local/share/fm-telegram/fm_telegram.py notify - \
  --choice '1:a=Open now' --choice '1:b=Later' \
  --choice '2:y=Yes' --choice '2:n=Hold'
```

This sends one [inline keyboard](https://core.telegram.org/bots/api#inlinekeyboardmarkup).
Each permitted press arrives as a Telegram
[callback query](https://core.telegram.org/bots/api#callbackquery); the bridge accepts it only
when the callback message is in `owner_chat_id`, calls
[answerCallbackQuery](https://core.telegram.org/bots/api#answercallbackquery) with a visible
toast (`받았어요 ✓ 1b`, `이미 받았어요`, or an expiry hint), and replaces that question's row
with its `✅` selection through
[editMessageReplyMarkup](https://core.telegram.org/bots/api#editmessagereplymarkup). It creates
one inbox note immediately per selected question, such as `Decision reply to Ab3dEf: 1b`, followed
by the question and label. A selected row is locked, so there is no extra **send** button.

Pending choices are held in a separate private, atomically-written state file and expire after
seven days. The running bridge reads that file fresh for each callback, so a concurrent `notify`
cannot be overwritten by the polling state save. A replayed callback uses its Telegram callback id
as the stable inbox request id, so retries do not create a second note. Plain `notify` calls are
unchanged.

### Verify a decision button after deploy

This safe live check sends one test button only to the configured owner chat. Press it and confirm
that Telegram shows `받았어요 ✓ 1b`, the button gains `✅`, and a matching Firstmate inbox note
appears.

```sh
python3 ~/.local/share/fm-telegram/fm_telegram.py notify 'Button delivery check' \
  --choice '1:b=Received'
```

Replies render a small Markdown subset as Telegram HTML: `**bold**`, `*italic*`, inline and fenced code, `https://` links and bullet lists; Markdown tables become `• column: value` lines. Any other `<`, `>` and `&` is escaped. Long replies are split at paragraph or line boundaries, and if Telegram rejects the formatting the same part is resent as plain text. See [reply style](docs/reply-style.md) and [readability research](docs/readability-research.md).

## Configuration

The config file is JSON at `~/.config/fm-telegram/config.json` (or `$XDG_CONFIG_HOME/fm-telegram/config.json`, `$FM_TELEGRAM_CONFIG`, or `--config`). It must be owned by you and not group/world writable, because it decides which `fm-inbox.sh` the bridge runs; otherwise the bridge refuses to start. Earlier versions kept it at `~/.local/state/fm-telegram/config.json`, which is still read when the new path does not exist.

| Key | Default | Meaning |
|-----|---------|---------|
| `owner_chat_id` | — (required) | The only Telegram chat id the bridge accepts. |
| `firstmate_home` | — (required unless `FM_HOME`/`--home`) | Firstmate home containing `bin/fm-inbox.sh`. `~` is expanded. |
| `poll_interval_seconds` | `2` | Pause between polls. Each poll also long-polls Telegram for up to 30 s. |
| `doorbell` | `true` | Wake the Firstmate pane through `herdr` when a new note arrives. |
| `doorbell_agent` | `null` | Only ring a pane whose agent, as `herdr pane list` reports it, has this name. `null` means any agent pane whose cwd is `firstmate_home`. |
| `denylist_file` | `null` | Text file of extra terms to redact from outbound messages, one per line, `#` comments allowed. |
| `keychain_service` | `"firstmate-telegram-bot"` | macOS Keychain service name for the token (account is `$USER`). |
| `api_base` | `"https://api.telegram.org"` | Bot API base URL. Must be `https://`; `http://` is accepted only for `localhost`/`127.0.0.1` test servers. |
| `whisper_cli` | `whisper-cli` on `PATH` | whisper.cpp command-line binary. |
| `whisper_model` | `~/.local/share/fm-telegram/models/ggml-large-v3-turbo-q5_0.bin` | whisper.cpp model file. |
| `whisper_language` | `"auto"` | Language passed to `whisper-cli -l`, such as `"en"` or `"ko"`. |
| `transcript_prefixes` | `[]` | Words whose spoken form "word, …" is rewritten to "word: …", so a dictated `todo: buy milk` matches a typed one. |
| `fleet_alerts` | `false` | Send alerts from Firstmate's fleet ledger (below). |
| `fleet_alert_labels` | `{}` | Replace alert labels, keyed `pr_ready`, `needs-decision`, `blocked`, `failed`, `done`. |
| `main_stall_alerts` | `false` | Alert when a pending captain note exceeds the threshold or the Firstmate watcher is reported down. |
| `main_stall_threshold_seconds` | `600` | Pending-note age that triggers the main-stall alert. |
| `claude_probe` | `false` | Run the bounded Claude login/response probe below. |
| `claude_probe_interval_seconds` | `3600` | Time between completed Claude probes. |
| `claude_probe_timeout_seconds` | `90` | Maximum time for one Claude probe before it is killed and reported as hung. |
| `claude_probe_command` | `["claude", "auth", "status"]` | Command array for the no-model probe. |
| `claude_auth_status_command` | `["claude", "auth", "status"]` | Command array checked after a failed probe for a logged-out state. |
| `oss_watch` | — | Settings for the optional GitHub watcher (below). |

| Environment variable | Meaning |
|----------------------|---------|
| `FM_TELEGRAM_BOT_TOKEN` | Bot token; takes precedence over the Keychain. |
| `FM_TELEGRAM_CONFIG` | Config file path. |
| `FM_TELEGRAM_STATE_DIR` | State directory (default `$XDG_STATE_HOME/fm-telegram`, i.e. `~/.local/state/fm-telegram`). |
| `FM_HOME` | Firstmate home; overrides `firstmate_home`. |
| `FM_TELEGRAM_LABEL_PREFIX` | LaunchAgent label prefix used by `install.sh` (default `io.github.fm-telegram`). |

### Files

| Path | Mode | Contents |
|------|------|----------|
| `~/.config/fm-telegram/config.json` | 0600 (you create it) | Configuration. No secrets. |
| `~/.local/state/fm-telegram/` | 0700 | State directory. |
| `…/state.json` | 0600 | Update offset, reply cursor, ids of notes created from Telegram, ids of the last 1000 updates turned into notes (so a redelivered update gets no second download or receipt), and, with `fleet_alerts`, the fleet ledger offset. Saves merge only changed keys under a lock so another process cannot erase them. |
| `…/pending-choices.json` | 0600 | Pending inline-choice state (for up to seven days), written atomically by `notify` and read fresh for every callback. |
| `…/sent-prs.txt` | 0600 | With `fleet_alerts`: PR URLs already sent to you, one per line. Never pruned. |
| `…/media/` | 0700, files 0600 | Downloaded attachments, named `<update id>-<file unique id>[-<name>].<ext>`. Never pruned automatically. |
| `~/.local/share/fm-telegram/` | 0700 | Installed programs and (optionally) the whisper model. |

The bot token is never written to any of these files or to logs.

## Voice transcription

Voice, audio and video messages are converted with macOS's built-in `/usr/bin/afconvert` to 16 kHz mono WAV in a temporary directory (it decodes Telegram's OGG/Opus and MP4, so ffmpeg is not needed), then transcribed with whisper.cpp. Nothing is sent to a cloud service. If transcription fails or is not installed, the note still arrives with `(transcription failed — check the attached audio)` and the attachment.

One-time setup on macOS:

```sh
brew install whisper-cpp
mkdir -p ~/.local/share/fm-telegram/models
curl -fL -o ~/.local/share/fm-telegram/models/ggml-large-v3-turbo-q5_0.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin
```

On Apple Silicon, a 7-second clip takes about 7 s on the first run (model loading) and about 1 s afterwards. Set `whisper_language` if auto-detection guesses wrong for short clips.

## Optional: fleet alerts

Without this, news about Firstmate's tasks reaches your phone only when the Firstmate session is in a turn and calls `notify`. With `"fleet_alerts": true` the bridge reads [Firstmate's fleet activity ledger](https://github.com/kunchenguid/firstmate/blob/main/docs/fleet-ledger.md) (`<firstmate_home>/state/fleet-ledger.jsonl`) itself on every poll. Alerts arrive even while Firstmate is idle or disconnected.

1. Turn the ledger on in the Firstmate home: `touch <firstmate_home>/config/fleet-ledger`.
2. Add `"fleet_alerts": true` to the config and restart the bridge.

| Ledger record | Alert |
|---------------|-------|
| `task.pr_ready` | `✅ PR ready for review · <task>` and the PR URL |
| `task.status`, state `needs-decision` | `❓ Decision needed · <task>` and the status text |
| `task.status`, state `blocked` | `⛔ Blocked · <task>` and the status text |
| `task.status`, state `failed` | `❌ Failed · <task>` and the status text |
| `task.status`, state `done` | `🏁 Finished · <task>` and the status text (a finished investigation, or a worker's PR) |

Nothing else alerts: dispatch, `working`/`paused`/`resolved` lines, merges, cleanups and unknown events are skipped. Status text is cut to 600 characters, then redacted and sent like any other message. To change the wording, set `fleet_alert_labels`, for example `{"blocked": "⛔ 막힘"}`.

- **No history flood, no repeats.** On the first poll after you enable it, the bridge starts at the end of the ledger. After that it saves a byte offset in `state.json` after each record, so a restart continues where it stopped. If the ledger is truncated, it is read again from its start. A partial last line waits for its newline.
- **One notice per PR.** A PR URL is sent once. A worker's `done: PR <url>` line usually arrives first, so the later `task.pr_ready` for the same URL is skipped, and so is a re-registration. While `fleet_alerts` is on, `notify` also records the PR URLs in its text: if Firstmate tells you about a PR first, the ledger alert for it is skipped.
- **Possible double notices.** Other events have no shared key. If Firstmate also sends a `notify` for a blocker, a decision or a PR it learned about after the alert, you get both. The ledger is at-least-once, so a rare repeated record can also repeat an alert.
- **Trust.** The ledger copies status text verbatim from Firstmate's `state/`. Workers write those lines, and the alert is only as private as they are.

## Optional: main and Claude health checks

Both health checks are off by default.
Enable them only for a Firstmate home whose bridge can already read its public `fm-inbox.sh` commands.

```json
{
  "main_stall_alerts": true,
  "main_stall_threshold_seconds": 600,
  "claude_probe": true,
  "claude_probe_interval_seconds": 3600,
  "claude_probe_timeout_seconds": 90
}
```

With `main_stall_alerts`, the bridge calls `fm-inbox.sh receipts --all-pending`, `ready`, and `status` on its normal polling loop.
It alerts once when the oldest pending captain note has exceeded the threshold or the public status reports that the watcher is down, then sends one recovery notice after both conditions clear.
The episode state is kept in the bridge state file, so restarting the bridge does not repeat an active alert.

With `claude_probe`, the bridge starts the configured `claude auth status` command in a background thread at the configured interval.
The default avoids model calls and quota use; set `claude_probe_command` to a `claude -p` command only when an opt-in model-response check is needed.
The command is killed at its timeout, so it never pauses Telegram polling.
A default probe that exits non-zero or reports `loggedIn: false`, or a custom command that reports login/authentication failure, sends `로그인 필요` with the `claude auth login` hint.
A timeout sends `Claude 멈춤`; every failure type sends one alert and one recovery notice when a later probe succeeds.

## Optional: GitHub activity watcher

`oss_watch.py` posts GitHub activity to the same chat through `fm_telegram.py notify`, reusing its token handling and redaction. It reads GitHub with the `gh` CLI, so run `gh auth login` first. Add a section to the config:

```json
"oss_watch": {
  "repositories": ["you/project"],
  "author": "you",
  "keywords": ["project"],
  "tracked": ["someone/awesome-list#123"]
}
```

- `check` (every 30 minutes as a LaunchAgent) reports new stars, forks, issues and PRs in `repositories`, and new comments, reviews and closures on issues/PRs by `author` that mention one of `keywords`, plus every `tracked` item. The first run only records a baseline; no change means no message.
- `weekly` (Mondays 08:00) summarises stars, forks, 14-day views and top referrers (the traffic API needs push access) and the state of tracked PRs.

```sh
python3 ~/.local/share/fm-telegram/oss_watch.py check
./install.sh --agent oss   # macOS: load both LaunchAgents
```

State is kept in `~/.local/state/fm-telegram/oss-watch.json` (0600).

## Security model

- **Who can talk to it:** only `owner_chat_id`. Other chats are ignored before any download, note or reply; only their numeric id is logged.
- **What runs:** no shell is ever used. Message text reaches `fm-inbox.sh` on stdin; the only argument derived from Telegram is the integer update id. Downloads are written only inside `media/` under generated names.
- **Secrets:** the token is read from the Keychain or the environment at start-up, used only in request URLs, and redacted from logs and outbound messages along with common token formats.
- **Network:** outbound HTTPS to `api.telegram.org` only. No listening socket.

Telegram is convenient, not confidential: messages are stored on Telegram's servers and bot chats are not end-to-end encrypted. Do not send customer data, raw logs, diffs, private paths or secrets through it; read those on the machine itself (for example over SSH). The redaction filter is a safety net, not permission to send sensitive text.

See [SECURITY.md](SECURITY.md) for reporting vulnerabilities and [docs/security-review.md](docs/security-review.md) for the full review.

## Development

The tests use fakes only: no real Telegram API, bot token or Firstmate.

```sh
python3 -m unittest discover -s tests -v
```

`tests/test_end_to_end.py` runs the real HTTP client against `tests/fake_telegram_api.py`, a local stand-in for the Bot API that you can also start by hand (`python3 tests/fake_telegram_api.py --token <any> --port 8765`) and point `api_base` at.

### Try it without Telegram

Run the whole flow on your machine against the fake Bot API, with a fake token and a Firstmate home:

```sh
export FM_TELEGRAM_BOT_TOKEN=123:fake
python3 tests/fake_telegram_api.py --token "$FM_TELEGRAM_BOT_TOKEN" --port 18765 &
```

Write the config as in setup step 4 with `"owner_chat_id": 1`, and add `"api_base": "http://127.0.0.1:18765"` and `"doorbell": false`. Then start the bridge, queue a message from chat 1 and read what the bridge sent back:

```sh
python3 ~/.local/share/fm-telegram/fm_telegram.py run &
curl -X POST localhost:18765/_control/updates \
  -d '{"update_id":1,"message":{"message_id":1,"date":0,"chat":{"id":1,"type":"private"},"text":"hi"}}'
curl localhost:18765/_control/sent
```

`fm-inbox.sh list` in your Firstmate home now shows the `hi` note.

## Tested

Last run 2026-10-17 on macOS (Apple Silicon, Python 3.8), following only this README in a throwaway home, with a fake Bot API instead of Telegram and no real token:

1. `HOME=$(mktemp -d)` with a minimal environment (`env -i`, no `FM_*`/`XDG_*` variables), `FM_TELEGRAM_BOT_TOKEN` set to a fake token, and a fresh clone of Firstmate at `~/firstmate`.
2. Setup step 3: `git clone` + `./install.sh` → both programs in `~/.local/share/fm-telegram/` with mode 0700; nothing loaded.
3. Step 4: wrote the config with `owner_chat_id: 0`, plus `api_base: "http://127.0.0.1:18765"` (`python3 tests/fake_telegram_api.py --token <fake> --port 18765`) and `doorbell: false`. Config mode 0600.
4. Step 5: ran the bridge and queued a message from chat 777001 → log line `ignored Telegram message from chat id=777001`, nothing sent. Set `owner_chat_id` to 777001 and restarted.
5. Queued a text, a document named `../../report.txt`, a voice note (macOS `say` → AAC, `whisper_language: "en"`), a sticker, and a message from a stranger chat. Result: four Firstmate notes (`fm-inbox.sh list`) — the text; `see attached` + `attachment: …/media/102-AgADdoc1-report.txt`; `To do, buy more coffee beans.` + `(voice)` + attachment; and `(attachment not received: sticker, size=2KB — unsupported type)`. Four receipts went to chat 777001 only; the stranger got nothing and only its id was logged. State dir and `media/` 0700, files 0600.
6. `fm-inbox.sh reply <first note> "**Build** is green. Leaked token: <the fake token>"` → the bridge sent `<b>Build</b> is green. Leaked token: [redacted]` with `parse_mode: HTML`.
7. `fm_telegram.py notify 'Deploy finished'` → delivered, exit 0.
8. Reset the saved offset to 0 and restarted, so every update was redelivered → no duplicate notes. (The only new note was the step-4 message, which had been ignored before 777001 became the owner.)
9. The fake token appeared in no file under `~/.config`, `~/.local/state` or the bridge logs. `python3 -m unittest discover -s tests` in the clone: 55 tests OK.

Not covered by that run: the real Telegram service, the Keychain path (`security`), LaunchAgents (`./install.sh --agent …`) and the Herdr doorbell, which have unit tests with fakes only.

## License

[MIT](LICENSE)
