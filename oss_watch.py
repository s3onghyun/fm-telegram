#!/usr/bin/env python3
"""Optional GitHub activity watcher that reports changes through fm_telegram.py notify."""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import fm_telegram

STATE_NAME = "oss-watch.json"


class GitHubError(RuntimeError):
    pass


class Gh:
    """Small JSON-only gh wrapper.  Arguments are always passed as a list."""
    def __init__(self, runner: Callable[..., Any] = subprocess.run) -> None:
        self.runner = runner

    def json(self, args: list[str]) -> Any:
        try:
            result = self.runner(["gh", *args], text=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, check=False)
        except OSError as exc:
            raise GitHubError("gh unavailable") from exc
        if result.returncode:
            raise GitHubError("gh request failed")
        try:
            return json.loads(result.stdout)
        except ValueError as exc:
            raise GitHubError("gh returned invalid JSON") from exc


class WatchState:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.path = directory / STATE_NAME

    def load(self) -> Optional[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (FileNotFoundError, ValueError):
            return None

    def save(self, data: dict[str, Any]) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)


def full_name(item: dict[str, Any]) -> str:
    repository = item.get("repository") or {}
    return str(repository.get("nameWithOwner") or repository.get("full_name") or "")


def item_key(item: dict[str, Any]) -> str:
    return "%s#%s" % (full_name(item), item.get("number"))


class Watcher:
    def __init__(self, gh: Gh, state: WatchState,
                 notify: Optional[Callable[[str], None]] = None,
                 logger: Optional[logging.Logger] = None,
                 repositories: Iterable[str] = (), author: Optional[str] = None,
                 keywords: Iterable[str] = (), tracked: Iterable[str] = ()) -> None:
        self.gh, self.state = gh, state
        self.repositories, self.author = list(repositories), author
        self.keywords = list(keywords)
        # "owner/repo#123" items to follow even when search no longer returns them.
        self.tracked = {(item.rsplit("#", 1)[0], int(item.rsplit("#", 1)[1])) for item in tracked}
        self.notify = notify or notify_via_bridge
        self.log = logger or logging.getLogger("oss-watch")

    def snapshot(self) -> dict[str, Any]:
        repos: dict[str, Any] = {}
        for repo in self.repositories:
            info = self.gh.json(["api", "repos/" + repo])
            issues = self.gh.json(["issue", "list", "--repo", repo, "--state", "all", "--limit", "100",
                                   "--json", "number,state,title,closedAt"])
            pulls = self.gh.json(["pr", "list", "--repo", repo, "--state", "all", "--limit", "100",
                                  "--json", "number,state,title,closedAt"])
            repos[repo] = {
                "stars": int(info.get("stargazers_count", 0)), "forks": int(info.get("forks_count", 0)),
                "issues": {str(x["number"]): str(x.get("state", "")).upper() for x in issues},
                "prs": {str(x["number"]): str(x.get("state", "")).upper() for x in pulls},
            }
        listings = self.listings()
        return {"version": 1, "repos": repos, "listings": listings}

    def listings(self) -> dict[str, Any]:
        fields = "number,url,title,body,state,repository,closedAt"
        found: list[dict[str, Any]] = []
        for kind in ("prs", "issues") if self.author else ():
            rows = self.gh.json(["search", kind, "--author", self.author, "--limit", "100", "--json", fields])
            for row in rows:
                repo, number = full_name(row), row.get("number")
                text = " ".join(str(row.get(part, "")) for part in ("title", "body", "url", "repository"))
                if (repo, number) in self.tracked or any(word.lower() in text.lower() for word in self.keywords):
                    row["_kind"] = kind
                    found.append(row)
        # A tracked item may no longer be returned by search after ownership or indexing changes.
        existing = {(full_name(row), row.get("number")) for row in found}
        for repo, number in sorted(self.tracked - existing):
            item = self.gh.json(["api", "repos/%s/issues/%d" % (repo, number)])
            item["repository"] = {"nameWithOwner": repo}
            item["_kind"] = "prs" if item.get("pull_request") else "issues"
            found.append(item)
        snapshots: dict[str, Any] = {}
        for row in found:
            repo, number = full_name(row), int(row["number"])
            key = "%s#%d" % (repo, number)
            comments = self.gh.json(["api", "repos/%s/issues/%d/comments" % (repo, number)])
            reviews: list[dict[str, Any]] = []
            if row["_kind"] == "prs":
                reviews = self.gh.json(["api", "repos/%s/pulls/%d/reviews" % (repo, number)])
            snapshots[key] = {"state": str(row.get("state", "OPEN")).upper(), "kind": row["_kind"],
                              "comments": [str(x.get("id")) for x in comments],
                              "reviews": [str(x.get("id")) for x in reviews]}
        return snapshots

    @staticmethod
    def changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
        messages: list[str] = []
        for repo, new in after["repos"].items():
            old = before["repos"].get(repo, {})
            star_delta = new["stars"] - int(old.get("stars", new["stars"]))
            fork_delta = new["forks"] - int(old.get("forks", new["forks"]))
            if star_delta > 0:
                messages.append("%s ★ +%d (%d)" % (repo, star_delta, new["stars"]))
            if fork_delta > 0:
                messages.append("%s forks +%d (%d)" % (repo, fork_delta, new["forks"]))
            for kind, label in (("issues", "issue"), ("prs", "PR")):
                old_items, new_items = old.get(kind, {}), new[kind]
                for number in sorted(set(new_items) - set(old_items), key=int):
                    messages.append("%s %s #%s opened" % (repo, label, number))
                for number, state in new_items.items():
                    if old_items.get(number) == "OPEN" and state != "OPEN":
                        messages.append("%s %s #%s closed" % (repo, label, number))
        old_listings, new_listings = before.get("listings", {}), after["listings"]
        for key, new in new_listings.items():
            old = old_listings.get(key, {})
            for field, label in (("comments", "comment"), ("reviews", "review")):
                count = len(set(new[field]) - set(old.get(field, [])))
                if count:
                    messages.append("%s %d new %s%s" % (key, count, label, "" if count == 1 else "s"))
            if old.get("state") == "OPEN" and new["state"] != "OPEN":
                if new["kind"] == "prs":
                    messages.append("%s %s" % (key, "merged" if new["state"] == "MERGED" else "closed"))
                else:
                    messages.append("%s closed" % key)
        return messages

    def check(self) -> bool:
        try:
            current = self.snapshot()
        except GitHubError as exc:
            self.log.warning("GitHub check failed: %s", exc)
            return False
        previous = self.state.load()
        if previous is None:
            self.state.save(current)
            return False
        changes = self.changes(previous, current)
        if changes:
            self.notify("📣 OSS activity\n" + "\n".join("• " + line for line in changes))
            self.state.save(current)
            return True
        self.state.save(current)
        return False

    def weekly(self) -> bool:
        try:
            rows: list[str] = ["📊 Weekly OSS summary"]
            for repo in self.repositories:
                info = self.gh.json(["api", "repos/" + repo])
                views = self.gh.json(["api", "repos/%s/traffic/views" % repo])
                rows.append("%s: ★%d · forks %d · views %d" %
                            (repo, int(info.get("stargazers_count", 0)), int(info.get("forks_count", 0)),
                             int(views.get("count", 0))))
                referrers = self.gh.json(["api", "repos/%s/traffic/popular/referrers" % repo])
                notable = ["%s(%s)" % (x.get("referrer"), x.get("count")) for x in referrers[:3] if x.get("count")]
                if notable:
                    rows.append("  referrers: " + ", ".join(notable))
            listings = self.listings()
        except GitHubError as exc:
            self.log.warning("GitHub weekly summary failed: %s", exc)
            return False
        prs = [key for key, value in listings.items() if value["kind"] == "prs"]
        open_prs = [key for key in prs if listings[key]["state"] == "OPEN"]
        closed_prs = [key for key in prs if listings[key]["state"] != "OPEN"]
        rows.append("Tracked PRs: open %d · merged/closed %d" % (len(open_prs), len(closed_prs)))
        if open_prs:
            rows.append("Open: " + ", ".join(open_prs))
        self.notify("\n".join(rows))
        return True


def notify_via_bridge(text: str) -> None:
    script = Path(__file__).with_name("fm_telegram.py")
    result = subprocess.run([sys.executable, str(script), "notify", "-"], input=text, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise RuntimeError("Telegram notification failed")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=fm_telegram.DEFAULT_CONFIG)
    parser.add_argument("--state-dir", type=Path, default=fm_telegram.DEFAULT_STATE)
    parser.add_argument("command", choices=("check", "weekly"))
    args = parser.parse_args(argv)
    log = logging.getLogger("oss-watch")
    try:
        settings = fm_telegram.load_config(args.config, args.state_dir / "config.json").get("oss_watch")
        if not isinstance(settings, dict) or not (settings.get("repositories") or settings.get("author")
                                                  or settings.get("tracked")):
            raise RuntimeError("add an oss_watch section to the config (see README)")
        watcher = Watcher(Gh(), WatchState(args.state_dir), repositories=settings.get("repositories", []),
                          author=settings.get("author"), keywords=settings.get("keywords", []),
                          tracked=settings.get("tracked", []))
        watcher.check() if args.command == "check" else watcher.weekly()
    except (RuntimeError, ValueError, IndexError) as exc:  # Index/ValueError: bad "tracked" item.
        log.warning("oss-watch failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
