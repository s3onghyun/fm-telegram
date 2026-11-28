# Contributing

Thanks for helping. fm-telegram is one Python file plus a test suite, standard library only, so you can work on it on Linux or macOS without a Mac-specific setup, a Telegram bot or a Firstmate home.

## Find something to work on

- Small, start-ready tasks: [open `good first issue` issues](https://github.com/s3onghyun/fm-telegram/issues?q=is%3Aopen+label%3A%22good+first+issue%22). Each one lists the files to touch, how to run it, when it is done and its size.
- Larger tasks that need some design: issues labeled `help wanted`.

**Claiming:** comment on the issue that you would like to take it. We assign the issue to you when we accept the claim; please wait for the assignment before opening a pull request, so two people do not do the same work. If an issue is already assigned, pick another one.

## Set up

Requirements: Python 3.8 or newer and `git`. Nothing to install.

```sh
git clone https://github.com/<you>/fm-telegram && cd fm-telegram
python3 -m unittest discover -s tests -v
```

The tests use fakes only (a local stand-in for the Bot API in `tests/fake_telegram_api.py`, fake Keychain and service runners); they need no real token, network access or Firstmate. To run one file, use for example `python3 -m unittest tests.test_setup -v`. To try the whole bridge by hand against the fake Bot API, follow [Try it without Telegram](README.md#try-it-without-telegram).

The suite currently expects `USER` to be set, as it is in a normal login shell ([#25](https://github.com/s3onghyun/fm-telegram/issues/25) removes that).

## Make a change

1. Fork the repository and create a branch.
2. Keep the change small and focused on one issue. Standard library only: no new dependencies.
3. Add or update a test in `tests/` for any behaviour change, and run `python3 -m unittest discover -s tests -v`.
4. Update `README.md` when you change a command, option or config key.
5. Open a pull request that says what changed, why, and how you tested it, and links the issue (`Fixes #<number>`).

Never put a real bot token, chat id, message content or private paths in code, tests, issues or pull requests; use fake values like the existing tests do. Report security problems privately as described in [SECURITY.md](SECURITY.md).

## What CI does

Every pull request, including one from a fork, runs `.github/workflows/tests.yml`: `python3 -m unittest discover -s tests -v` on `ubuntu-latest` and `macos-latest`. The workflow has read-only permissions and uses no secrets. For a first-time contributor, a maintainer approves the workflow run before it starts, so the checks may show as waiting for a moment. A maintainer reviews and merges once the checks are green.
