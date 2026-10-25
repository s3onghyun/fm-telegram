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
