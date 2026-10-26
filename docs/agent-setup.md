# Setting up fm-telegram with a coding agent

These instructions are for a coding agent (Claude Code, Codex, Pi, …) that a person has asked to "set up fm-telegram". The person already runs [Firstmate](https://github.com/kunchenguid/firstmate). Everything except three steps is done by the agent.

## What the person does

1. **Create the bot.** In Telegram, open [@BotFather](https://t.me/BotFather), send `/newbot`, pick a name, and keep the token it shows.
2. **Enter the token once**, in their own terminal (the input is hidden, so the agent never sees it).
3. **Send the bot one message** from their own account when setup asks for it.

## What the agent does

Never ask the person to paste the token into the chat, never pass it as an argument, and never print it. `setup` is safe to run again at any point; every run re-checks everything and changes only what is missing.

1. **Get the code.** Clone it somewhere outside `~/Documents`, `~/Desktop` and `~/Downloads`:

   ```sh
   git clone https://github.com/s3onghyun/fm-telegram.git ~/fm-telegram
   ```

2. **Find the Firstmate home**: the Firstmate clone that contains `bin/fm-inbox.sh` (often `$FM_HOME` or `~/firstmate`). Ask the person if you cannot find it.

3. **Run setup** without a terminal prompt:

   ```sh
   python3 ~/fm-telegram/fm_telegram.py setup --firstmate-home <home> --yes
   ```

   Each progress line reads `setup <step> <status>: <message>`. Exit codes: `0` done, `3` a person must act (the `need-human` line says what; afterwards run the same command again), `1` failed (the `fail` line says why).

4. **Token** (`setup token need-human`). Tell the person to create the bot (step 1 above) and then run this in their own terminal, outside your session:

   ```sh
   python3 ~/fm-telegram/fm_telegram.py setup --token-only
   ```

   It asks for the token with hidden input, checks it with Telegram, and stores it in the macOS Keychain (on Linux in `~/.config/fm-telegram/runtime.env`, mode 0600). When they say it is done, run step 3 again.

5. **Owner chat** (`setup owner need-human: send any message to @<bot> …`). Tell the person to send the bot any message now, from a **private** chat. Setup waits up to 5 minutes (`--wait` changes this), takes the first private message, and prints whose it is. Ask the person to confirm that name is theirs; if not, delete `owner_chat_id` from `~/.config/fm-telegram/config.json` and run step 3 again.

6. **Run mode.** With `--yes`, setup installs a LaunchAgent (macOS) or a systemd user service (Linux) and starts it, unless the Firstmate home is under `~/Documents`, `~/Desktop`, `~/Downloads` or iCloud Drive. macOS privacy protection hides those folders from LaunchAgents, so setup then prints `setup run-mode warn` and a `setup run-mode action` line with the command to keep running instead. Start it in a pane that stays open (tmux or Herdr), for example the `tmux new-session …` command from that line. Use `--run-mode terminal` to always get that command, `--run-mode none` to install nothing.

7. **Self-test.** Setup sends "fm-telegram is connected ✅" to the person. With a service it then waits for the person to send any message and reports `setup done ok: round trip works`. Otherwise, once the bridge runs, ask the person to send a message: the bot's `Received. Note ID: <id>` answer means the note reached `fm-inbox`, and `bin/fm-inbox.sh list` in the Firstmate home shows it.

8. **Optional voice notes** (macOS). `setup voice optional` lists the exact commands (`brew install whisper-cpp` and a ~0.5 GB model download). Ask before running them.

Finish by telling the person the bot's `@username`, the run mode, and that `/help` in the chat lists what the bot accepts.
