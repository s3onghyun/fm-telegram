# Standby fallback on a second machine

A laptop that runs the bridge is often asleep or offline. Telegram keeps undelivered updates for about 24 hours, so nothing is lost, but nothing is answered either. A standby bridge on an always-on machine (a home mini PC, say) covers that gap:

- it tells you once per message that the main machine is away (`맥이 자고 있어요 💤 깨면 처리할게요` by default);
- a message that calls a configured name, such as `미니야 …` or `/m …`, becomes a note in a secondmate's Firstmate home on the standby machine right away ([Calling a secondmate by name](#calling-a-secondmate-by-name));
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

Named messages are delivered by the standby itself with the routed home's own `bin/fm-inbox.sh note --request-id tg:<update id>`; the standby never writes another home's files. Attachments of routed messages are saved in the standby's `media/` directory. Queued messages keep their Telegram `file_id`, and the primary downloads the attachment when it processes the queue.

### Edge cases

- **The primary slept between handling a batch and confirming it.** The standby receives that batch again and queues it, and you get an away reply for messages that were already notes. The primary skips them on handoff, so nothing becomes a second note.
- **The primary cannot reach the standby.** It logs `standby handoff failed`, keeps polling and retries after `failover_interval_seconds`. Updates still queued on the standby are handed over on the next successful claim, after newer messages.
- **A decision button pressed while the primary was away** is answered with the away text and handed over like a message. The primary records the choice; its own toast arrives too late for Telegram and is skipped.
- **Replies.** The standby does not forward Firstmate replies from the routed homes; it only delivers notes to them. See [Answers from a secondmate](#answers-from-a-secondmate).

## Calling a secondmate by name

One name, `미니`, reaches the secondmates on the mini PC. The standby picks which of its homes gets the note.

| You send | Goes to |
|----------|---------|
| `미니야 …`, `미니아 …`, `미니, …`, `미니 …`, `/m …`, `/p …` | 미니. The call is removed from the note: `미니야 릴스 기획` becomes `릴스 기획`. |
| A Telegram reply to a message that starts with `[미니]` or with 미니's receipt | 미니 again, the same home that sent it. |
| Anything else, including `미니멀…`, `미니아폴리스…`, `인스타그램 링크` and `오늘 미니야…` | The main Firstmate, as before. |

A call counts only at the very start, as the name followed by `야` or `아` (optional) and then a space, punctuation or the end of the message. A bare `미니야` is delivered as it is, so a note is never empty. Captions of photos and files are matched the same way. Voice messages are not: their words are known only after transcription, so they go to main unless they reply to a 미니 message.

**Which home.** A reply goes back to the home that sent the replied-to message: the first line of that message names its home's tag, such as `(인스타 담당)`. Otherwise the first home whose `keywords` appear in the text (case-insensitive) gets it, and the route's own `home` gets everything else. In the example below, Instagram content (인스타, 캐러셀, 릴스, 카드뉴스, the four account names and their Korean titles) goes to insta-factory and the rest to the personal secondmate.

**Where it is delivered.**

| Primary (Mac) | Delivery |
|---------------|----------|
| awake | The primary sends the message to the standby with `failover_command … standby deliver` (the update as JSON on stdin, over the same SSH link as the heartbeat). The standby notes it in the chosen home and sends the receipt. |
| asleep | The standby is polling and does the same itself. |
| awake, but the standby is unreachable | The primary sends `⚠️ 미니에게 전달하지 못해서 메인에 남길게요` and notes the message in main, call included, with the usual `Received. Note ID`. Nothing is lost and later messages are not held up. |

**Receipts.** Main keeps `Received. Note ID: …` and the 👀/👌 reactions. A message for 미니 gets the route's reaction (👨‍💻 by default) and one line, `🖥️ → 미니에게 전달했어요`, with ` (인스타 담당)` added when a tagged home got it. Telegram accepts only [a fixed list of reaction emoji](https://core.telegram.org/bots/api#reactiontypeemoji); 🖥️ is not on it, so it lives in the receipt text, and a reaction Telegram rejects is logged and skipped.

**Main still knows.** Every message for 미니 is also written to the main Firstmate's inbox as a read-only copy that starts with `[FYI routed-to:미니]` (or `[FYI routed-to:미니/insta]`), under the same `tg:<update id>` request id. The bridge never relays a reply to that note, so it cannot cause a second Telegram message. When the Mac was asleep, the standby queues this FYI copy with the other messages and the primary records it on wake. In the other direction, a secondmate already reports to main through its parent status channel, so main sees both sides.

**Short commands.** At start-up the primary registers each route's `commands` with `/status` and `/help` through `setMyCommands`, so `/m` and `/p` appear in Telegram's command menu. `setMyCommands` replaces the whole menu; without routes it is left alone.

### Answers from a secondmate

A secondmate cannot send to Telegram itself today: its parent channel goes to the main Firstmate. When main passes a secondmate's answer on, it labels it:

```bash
python3 ~/.local/share/fm-telegram/current/fm_telegram.py notify --from 미니 '오늘 할 일 정리했어요'
python3 ~/.local/share/fm-telegram/current/fm_telegram.py notify --from 미니/insta - < answer.md
```

These arrive as `[미니] 오늘 할 일 정리했어요` and `[미니] (인스타 담당) …`. Replying to either routes your answer back to that home. An answer a secondmate records with its own `fm-inbox.sh reply` stays in its home; nothing relays it.

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
    "미니": {
      "commands": ["/m", "/p"],
      "receipt": "🖥️ → 미니에게 전달했어요",
      "home": "~/firstmate-homes/personal",
      "homes": {
        "insta": {
          "home": "~/firstmate-homes/insta-factory",
          "tag": "인스타 담당",
          "keywords": ["인스타", "캐러셀", "릴스", "카드뉴스",
                       "altearth", "deepfathom", "sacredframes", "wanderlore",
                       "만약의 지구", "깊은 곳의 지구", "그림으로 읽는 성경", "여행지의 사연"]
        }
      }
    }
  }
}
```

Put the same `standby_routes` block in the primary's config too. The primary uses its names, commands, labels and receipts to recognise messages for 미니 and replies to them; the home paths there are not used, since the standby owns the homes. A primary with `standby_routes` but no `failover_command` refuses to start.

| Key | Where | Default | Meaning |
|-----|-------|---------|---------|
| `failover_command` | primary | `null` (off) | Command array that runs `fm_telegram.py` on the standby. |
| `failover_interval_seconds` | primary | `60` | Heartbeat interval. Keep it well below the standby's `standby_stale_seconds`. |
| `standby_stale_seconds` | standby | `180` | Heartbeat age after which the standby polls. A restarted standby also waits this long. |
| `standby_reply` | standby | `맥이 자고 있어요 💤 깨면 처리할게요` | Away reply, sent once per queued message. |
| `standby_routes` | both | `{}` | Name → route (below). On the standby each home must contain `bin/fm-inbox.sh`, or it refuses to start. |

| Route key | Default | Meaning |
|-----------|---------|---------|
| `home` | required | Default Firstmate home on the standby. |
| `homes` | `{}` | Key → `{home, tag, keywords}`: the first home whose keyword appears in the text gets it. `tag` (default: the key) is shown as `(tag)` in receipts and labels. |
| `aliases` | `[]` | Other names that call the route. |
| `commands` | `[]` | Short commands such as `/m`: lowercase letters, digits or `_`, at most 32. |
| `label` | `[<name>]` | Prefix of relayed answers; a reply to a message starting with it routes back. |
| `emoji` | `👨‍💻` | Reaction on a delivered message; must be a Telegram reaction emoji. |
| `receipt` | `→ <name>에게 전달했어요` | Delivery line; a reply to it routes back too. |

| Standby file (in its state directory) | Mode | Contents |
|---------------------------------------|------|----------|
| `primary-heartbeat.json` | 0600 | The primary's offset; its modification time is the heartbeat. |
| `standby-queue.json` | 0600 | Queued owner updates, unchanged, until the primary acknowledges them. |
| `standby-active.lock`, `standby-queue.lock` | 0600 | Locks shared by the poller and `claim`/`ack`. |

## Deploy checklist

This covers the standby fallback and calling 미니 by name together; neither is deployed yet.

1. **Standby: install the release without a service.** `./deploy/install.sh` from a reviewed checkout that contains both changes.
2. **Standby: token and config.** Put `FM_TELEGRAM_BOT_TOKEN=…` in a mode-0600 `~/.config/fm-telegram/runtime.env` (from your secret manager, never through a chat), and write the standby config above with mode 0600. Check that `standby_routes.미니.home` and every `homes.*.home` contain `bin/fm-inbox.sh`.
3. **Primary: test the link as the service user.** `ssh -o BatchMode=yes standby-host python3 ~/.local/share/fm-telegram/current/fm_telegram.py standby claim 0` prints `{"offset": 0, "updates": []}`. A LaunchAgent has no interactive SSH agent: the key must work without a passphrase prompt.
4. **Primary: test delivery as the service user.** Pipe one fake update into `standby deliver` over the same link, using a throwaway update id and the owner chat id:
   `echo '{"route": "미니", "update": {"update_id": 1, "message": {"chat": {"id": OWNER}, "text": "배포 확인"}}}' | ssh -o BatchMode=yes standby-host python3 ~/.local/share/fm-telegram/current/fm_telegram.py standby deliver`
   prints `{"home": null}`, the personal home gets a `배포 확인` note and Telegram shows `🖥️ → 미니에게 전달했어요`. Acknowledge that note in the personal home afterwards.
5. **Primary: turn on `failover_command` and add the same `standby_routes` block**, then restart the bridge (on macOS, `./deploy/install.sh --launchd --python …` again). The log shows no `standby handoff failed` and no `setMyCommands failed`; `/m` and `/p` appear in the bot's command menu.
6. **Standby: start the service.** `./deploy/install.sh --systemd --standby` installs the `io.github.fm-telegram.fm-telegram-standby` user unit. On a headless machine run `loginctl enable-linger "$USER"` so it starts without a login. `journalctl --user -u io.github.fm-telegram.fm-telegram-standby` shows nothing beyond start-up while the primary heartbeats.
7. **Verify name calls with the Mac awake.** Send `미니야 오늘 할 일` and `/p 릴스 아이디어`. Each gets the 👨‍💻 reaction and one receipt line (the second with ` (인스타 담당)`), lands in the personal and insta-factory homes respectively, and appears in main's inbox as `[FYI routed-to:…]`. `미니멀 정리` still gets `Received. Note ID`. If the reaction is missing, the log names a rejected `emoji`: pick another from Telegram's list.
8. **Verify the failover.** Stop the primary's bridge, wait `standby_stale_seconds`, then send one plain message and one `미니야 …` message. You get the away reply and 미니's receipt; the standby log says `primary heartbeat is stale; polling Telegram`. Start the primary again: within one poll it sends `Received. Note ID` for the queued plain message (nothing for the 미니 message), main's inbox gets the FYI copy, and the standby log says `primary is back; standing by`.
9. **Verify reply routing.** From main, send `notify --from 미니/insta '배포 확인'`; reply to it in Telegram and check that the reply lands in insta-factory.

Rollback: `systemctl --user disable --now io.github.fm-telegram.fm-telegram-standby` on the standby, then remove `failover_command` and `standby_routes` from the primary's config and restart it. Undelivered queued updates stay in `standby-queue.json` until you run `standby claim` again. The command menu keeps `/m` and `/p` until the next `setMyCommands`; send it an empty list to clear them.
