# Deployment assets

These assets make a checked-out revision the source of a repeatable installation. They deliberately do **not** turn the bridge into a standalone service: every deployment still needs a compatible, writable Firstmate home containing `bin/fm-inbox.sh`.

## User-service installation

Run the installer from a checked-out, reviewed revision. It installs immutable release directories under the data directory and atomically changes `current`; a service reconciliation then restarts through that stable path. Re-running the same command is idempotent.

```sh
# Copy only; useful for a supervised process.
./deploy/install.sh

# macOS: uses the existing Keychain lookup. No token enters the plist.
./deploy/install.sh --launchd

# Linux: reconciles a systemd user unit.
./deploy/install.sh --systemd
```

Override `FM_TELEGRAM_DATA_DIR`, `FM_TELEGRAM_LABEL`, `--data-dir`, or `--version` for an isolated test. The installer uses the checked-out Git revision when no version is supplied. It never kills a PID or copies into an unversioned live program path.

On Linux, put `FM_TELEGRAM_BOT_TOKEN=...` in a secret-manager-managed, mode-0600 `~/.config/fm-telegram/runtime.env`; it is read by the systemd unit and must not be committed. On macOS the LaunchAgent intentionally does not set this variable, so the existing `keychain_service` lookup remains the token source.

## macOS: a dedicated interpreter for the LaunchAgent

macOS privacy protection (TCC) hides `~/Documents`, `~/Desktop`, `~/Downloads` and iCloud Drive from LaunchAgents until the user allows access, and it records that grant against the **launched binary**. The LaunchAgent therefore runs its interpreter directly (no `/usr/bin/env`), and `--python` (or `FM_TELEGRAM_PYTHON`) picks it. Without it the installer uses the `python3` on `PATH`; if your Firstmate home is in a protected folder, granting that shared `python3` access would extend it to every script it runs.

Give the bridge its own copy instead, for example from a [python-build-standalone](https://github.com/astral-sh/python-build-standalone) build that `uv python install` already downloaded:

```sh
rt=~/.local/share/fm-telegram/runtime
mkdir -p "$rt" && chmod 700 "$rt"
ditto ~/.local/share/uv/python/cpython-3.12.8-macos-aarch64-none "$rt/cpython-3.12.8"
cp "$rt/cpython-3.12.8/bin/python3.12" "$rt/cpython-3.12.8/bin/fm-telegram-python"
codesign -s - -f -i io.example.fm-telegram.python "$rt/cpython-3.12.8/bin/fm-telegram-python"
./deploy/install.sh --launchd --python "$rt/cpython-3.12.8/bin/fm-telegram-python"
```

The copy keeps the whole directory because the executable links its library relative to itself. The ad-hoc signature pins the grant to that binary. The first time the agent touches the protected folder macOS asks once whether **fm-telegram-python** may access it; allow it, and check later under System Settings → Privacy & Security → Files and Folders. Do not grant Full Disk Access. A new interpreter path or a re-signed binary is a new identity and asks again.

The plist runs with `ProcessType` `Standard` rather than `Background`, because voice transcription is work the owner is waiting for.

## macOS: moving a running loop to the LaunchAgent

`launchd-cutover.sh` moves a bridge that runs in a shell restart loop (for example `while true; do python3 …/fm_telegram.py run; sleep 5; done` in a Herdr pane) to the LaunchAgent without ever running two `getUpdates` pollers, which Telegram would answer with HTTP 409. Run it from the reviewed checkout to install:

```sh
export FM_TELEGRAM_PYTHON=~/.local/share/fm-telegram/runtime/cpython-3.12.8/bin/fm-telegram-python
export FM_TELEGRAM_PANE=w1:p1 FM_TELEGRAM_LOOP='while true; do …; done'   # for rollback
./deploy/launchd-cutover.sh check      # read-only preflight
./deploy/launchd-cutover.sh apply      # stop the loop, install, verify, or roll back
./deploy/launchd-cutover.sh rollback   # bootout, park the plist, restart the loop
```

| Step | What it does |
|---|---|
| `check` | Runtime present, ad-hoc signed, imports `ssl` and runs this checkout; the label is not loaded yet; at most one poller, run by a shell. |
| `apply` | `check`; stop the loop (SIGINT the bridge, then the loop's `sleep`); require zero pollers; `install.sh --launchd --python`; verify the agent keeps one pid for 15 s, is the only poller, logs no 409 or traceback, and that a message you send after its `notify` becomes a note. Any failure runs `rollback`. |
| `rollback` | `launchctl bootout`; wait for zero pollers; move the plist to `<data dir>/<label>.plist.rolled-back` so the next login does not start it; restart the loop with `herdr pane run`, or print it when `FM_TELEGRAM_PANE`/`FM_TELEGRAM_LOOP` are unset. |

`FM_TELEGRAM_LABEL`, `FM_TELEGRAM_DATA_DIR`, `FM_TELEGRAM_STATE_DIR`, `FM_TELEGRAM_MATCH` (poller command-line pattern, default `fm_telegram.py run`) and `FM_TELEGRAM_WAIT_NOTE` (seconds, default 300) override the defaults.

After the cutover, redeploy a new revision with the same `install.sh --launchd --python …` from the new checkout; it switches `current` and restarts the agent. Read the log at `~/Library/Logs/fm-telegram.log`.

The systemd unit has conservative process isolation only. A stricter filesystem sandbox cannot be enabled generically because `fm-inbox.sh` must write the mounted Firstmate home.

## Container and Kubernetes PoC shape

Build an image locally:

```sh
podman build -t fm-telegram:local -f Containerfile .
```

The image runs as UID 10001. Mount a writable `/state`, a read-only `/config/config.json`, and a **writable** `/firstmate` home. Set `FM_TELEGRAM_BOT_TOKEN` only through the runtime secret mechanism. A read-only root filesystem is supported because all writes go to `/state` or the Firstmate home.

`kubernetes/fm-telegram.yaml` is an intentionally local-only Pod/Deployment shape: it consumes a token from a `Secret`, state from a PVC, config from a ConfigMap, and uses `hostPath` for a local Firstmate home. Create the Secret outside Git, for example:

```sh
kubectl create secret generic fm-telegram --from-literal=token='...'
```

Before applying, set the ConfigMap's `owner_chat_id` and replace the illustrative image and `hostPath`. The included `doorbell: false` avoids assuming that a pod can access host-local Herdr.

Do not apply that manifest to a shared or company cluster. A container/pod cannot independently service this bridge until its Firstmate inbox interface and any desired doorbell capability are made networked, authenticated services.
