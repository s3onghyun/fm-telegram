# Standby fallback on a second machine

A laptop that runs the bridge is often asleep or offline. Telegram keeps undelivered updates for about 24 hours, so nothing is lost, but nothing is answered either. A standby bridge on an always-on machine (a home mini PC, say) covers that gap:

- it tells you once per message that the main machine is away (`맥이 자고 있어요 💤 깨면 처리할게요` by default);
- a message that starts with a configured prefix, such as `불침번:` or `인스타:`, becomes a note in that prefix's Firstmate home on the standby machine right away;
- every other message is queued unchanged and handed to the primary's inbox when it is back.

A bot has exactly one `getUpdates` consumer. When two pollers run, Telegram answers one of them with HTTP 409 and the two take turns stealing updates. The standby therefore never polls while the primary is alive.

## How the handoff works

```text
primary (laptop)                                         standby (mini PC, always on)
fm_telegram.py run                                       fm_telegram.py standby run
  │ every failover_interval_seconds, and before the       │ heartbeat fresh → idle
  │ first poll after waking:                              │ heartbeat older than standby_stale_seconds
  │   ssh standby fm_telegram.py standby claim <offset> ─►│   → poll, reply, route or queue
  │ ◄── {"offset": N, "updates": [queued updates]} ───────│
  │ process the queue, oldest first                       │
  │   ssh standby fm_telegram.py standby ack <last id> ──►│ drop the acknowledged updates
  │ getUpdates(offset ≥ N)                                │
```

The primary drives every step over SSH. The standby opens no port and never connects to the primary, so the primary needs no SSH server and can be asleep at any moment. It pushes its heartbeat instead of the standby probing for it, because a heartbeat proves that the bridge process is alive and polling, not only that the machine answers.

| Step | Guarantee |
|------|-----------|
| `claim` writes the heartbeat **first**, then waits for the standby's active lock (60 s at most). | Holding the lock means the standby finished its current poll. Afterwards it re-checks the heartbeat under the same lock, finds it fresh, and does not poll again. Only one poller runs. |
| The primary adopts the standby's offset before processing anything. | Its next `getUpdates(offset)` confirms every update the standby handled, so the primary never receives them a second time. |
| The heartbeat carries the primary's offset; the standby never polls below it. | Updates the primary already confirmed do not come back to the standby. |
| The queue is processed through the normal `process_update`, oldest first, before the next poll. | Owner-only allowlist, attachment download, voice transcription, `Received. Note ID` receipts and decision buttons behave exactly as if the message had just arrived. |
| Each queued update becomes `fm-inbox.sh note --request-id tg:<update id>`, and `ack` is sent only after the whole queue went through. | At least once, and idempotent: a failed handoff is retried before the next poll (keeping the order), and a repeated claim after a lost `ack` is skipped by `handled_update_ids`. |
| A standby that gets HTTP 409 stands by for `standby_stale_seconds`. | When the primary cannot reach the standby but is polling, the primary wins. |

Prefix routes are delivered by the standby itself with the routed home's own `bin/fm-inbox.sh note --request-id tg:<update id>`; the standby never writes another home's files. Attachments of routed messages are saved in the standby's `media/` directory. Queued messages keep their Telegram `file_id`, and the primary downloads the attachment when it processes the queue.

### Edge cases

- **The primary slept between handling a batch and confirming it.** The standby receives that batch again and queues it, and you get an away reply for messages that were already notes. The primary skips them on handoff, so nothing becomes a second note.
- **The primary cannot reach the standby.** It logs `standby handoff failed`, keeps polling and retries after `failover_interval_seconds`. Updates still queued on the standby are handed over on the next successful claim, after newer messages.
- **A decision button pressed while the primary was away** is answered with the away text and handed over like a message. The primary records the choice; its own toast arrives too late for Telegram and is skipped.
- **Replies.** The standby does not forward Firstmate replies from the routed homes; it only delivers notes to them.

## Configuration

On the primary, add the command that runs `fm_telegram.py` on the standby. The bridge appends `standby claim <offset>` or `standby ack <id>`.

```json
{
  "failover_command": ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "standby-host",
                       "python3", "~/.local/share/fm-telegram/current/fm_telegram.py"],
  "failover_interval_seconds": 60
}
```

On the standby, use the same bot token and `owner_chat_id`. `firstmate_home` is not needed.

```json
{
  "owner_chat_id": 123456789,
  "standby_stale_seconds": 180,
  "standby_reply": "맥이 자고 있어요 💤 깨면 처리할게요",
  "standby_routes": {
    "불침번:": "~/firstmate-homes/personal",
    "인스타:": "~/firstmate-homes/insta"
  }
}
```

| Key | Where | Default | Meaning |
|-----|-------|---------|---------|
| `failover_command` | primary | `null` (off) | Command array that runs `fm_telegram.py` on the standby. |
| `failover_interval_seconds` | primary | `60` | Heartbeat interval. Keep it well below the standby's `standby_stale_seconds`. |
| `standby_stale_seconds` | standby | `180` | Heartbeat age after which the standby polls. A restarted standby also waits this long. |
| `standby_reply` | standby | `맥이 자고 있어요 💤 깨면 처리할게요` | Away reply, sent once per queued message. |
| `standby_routes` | standby | `{}` | Text or caption prefix → Firstmate home on the standby. Each home must contain `bin/fm-inbox.sh`, or the standby refuses to start. |

| Standby file (in its state directory) | Mode | Contents |
|---------------------------------------|------|----------|
| `primary-heartbeat.json` | 0600 | The primary's offset; its modification time is the heartbeat. |
| `standby-queue.json` | 0600 | Queued owner updates, unchanged, until the primary acknowledges them. |
| `standby-active.lock`, `standby-queue.lock` | 0600 | Locks shared by the poller and `claim`/`ack`. |

## Deploy checklist

1. **Standby: install the release without a service.** `./deploy/install.sh` from a reviewed checkout.
2. **Standby: token and config.** Put `FM_TELEGRAM_BOT_TOKEN=…` in a mode-0600 `~/.config/fm-telegram/runtime.env` (from your secret manager, never through a chat), and write the standby config above with mode 0600. Check that every `standby_routes` home has `bin/fm-inbox.sh`.
3. **Primary: test the link as the service user.** `ssh -o BatchMode=yes standby-host python3 ~/.local/share/fm-telegram/current/fm_telegram.py standby claim 0` prints `{"offset": 0, "updates": []}`. A LaunchAgent has no interactive SSH agent: the key must work without a passphrase prompt.
4. **Primary: turn on `failover_command`** and restart the bridge (on macOS, `./deploy/install.sh --launchd --python …` again). The log shows no `standby handoff failed`.
5. **Standby: start the service.** `./deploy/install.sh --systemd --standby` installs the `io.github.fm-telegram.fm-telegram-standby` user unit. On a headless machine run `loginctl enable-linger "$USER"` so it starts without a login. `journalctl --user -u io.github.fm-telegram.fm-telegram-standby` shows nothing beyond start-up while the primary heartbeats.
6. **Verify the failover.** Stop the primary's bridge, wait `standby_stale_seconds`, then send one plain message and one routed message. You get the away reply and the routed receipt; the standby log says `primary heartbeat is stale; polling Telegram`. Start the primary again: within one poll it sends `Received. Note ID` for the queued message, and the standby log says `primary is back; standing by`.

Rollback: `systemctl --user disable --now io.github.fm-telegram.fm-telegram-standby` on the standby, then remove `failover_command` from the primary's config and restart it. Undelivered queued updates stay in `standby-queue.json` until you run `standby claim` again.
