import io
import json
import logging
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path

import oss_watch as ow

REPOSITORIES = ("example/one", "example/two")
TRACKED = ("upstream/list#2555",)

def snapshot(stars=10, forks=2, issue_state="OPEN", pr_state="OPEN", listings=None):
    repos = {}
    for repo in REPOSITORIES:
        repos[repo] = {"stars": stars, "forks": forks,
                       "issues": {"1": issue_state}, "prs": {"2": pr_state}}
    return {"version": 1, "repos": repos, "listings": listings or {
        "upstream/list#2555": {"state": "OPEN", "kind": "prs", "comments": ["1"], "reviews": ["2"]}
    }}


class FakeGh:
    def __init__(self, responses=None, error=False):
        self.responses, self.error, self.calls = responses or {}, error, []

    def json(self, args):
        self.calls.append(args)
        if self.error:
            raise ow.GitHubError("fake failure")
        key = tuple(args)
        value = self.responses.get(key)
        if value is None:
            raise AssertionError("missing fake gh response: %r" % (args,))
        return value


class SnapshotWatcher(ow.Watcher):
    def __init__(self, snapshots, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.snapshots = iter(snapshots)

    def snapshot(self):
        return next(self.snapshots)


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.messages = []
        self.state = ow.WatchState(Path(self.temp.name) / "state")

    def tearDown(self):
        self.temp.cleanup()

    def test_first_run_seeds_without_notifying_and_uses_private_state_dir(self):
        watcher = SnapshotWatcher([snapshot()], FakeGh(), self.state, self.messages.append)
        self.assertFalse(watcher.check())
        self.assertEqual([], self.messages)
        self.assertTrue(self.state.path.exists())
        self.assertEqual(0o700, os.stat(self.state.directory).st_mode & 0o777)

    def test_changes_cover_stars_forks_issues_prs_comments_reviews_and_closed_listing(self):
        before = snapshot()
        after = snapshot(stars=12, forks=3, issue_state="CLOSED", pr_state="CLOSED", listings={
            "upstream/list#2555": {"state": "CLOSED", "kind": "prs",
                                                 "comments": ["1", "3"], "reviews": ["2", "4"]}
        })
        after["repos"][REPOSITORIES[0]]["issues"]["9"] = "OPEN"
        after["repos"][REPOSITORIES[0]]["prs"]["10"] = "OPEN"
        self.state.save(before)
        watcher = SnapshotWatcher([after], FakeGh(), self.state, self.messages.append)
        self.assertTrue(watcher.check())
        message = self.messages[0]
        for expected in ("★ +2 (12)", "forks +1 (3)", "issue #9 opened", "PR #10 opened",
                         "issue #1 closed", "PR #2 closed", "1 new comment", "1 new review",
                         "upstream/list#2555 closed"):
            self.assertIn(expected, message)

    def test_unchanged_snapshot_sends_nothing(self):
        current = snapshot()
        self.state.save(current)
        watcher = SnapshotWatcher([current], FakeGh(), self.state, self.messages.append)
        self.assertFalse(watcher.check())
        self.assertEqual([], self.messages)

    def test_gh_failure_is_nonfatal_and_preserves_snapshot(self):
        self.state.save(snapshot())
        logger = logging.getLogger("oss-watch-test-%s" % id(self))
        logger.handlers[:] = [logging.StreamHandler(io.StringIO())]
        watcher = ow.Watcher(FakeGh(error=True), self.state, self.messages.append, logger,
                             repositories=REPOSITORIES)
        self.assertFalse(watcher.check())
        self.assertEqual(10, self.state.load()["repos"][REPOSITORIES[0]]["stars"])
        self.assertEqual([], self.messages)

    def test_weekly_format_uses_fake_gh_traffic_output(self):
        responses = {}
        for repo in REPOSITORIES:
            responses[("api", "repos/" + repo)] = {"stargazers_count": 8, "forks_count": 3}
            responses[("api", "repos/%s/traffic/views" % repo)] = {"count": 42}
            responses[("api", "repos/%s/traffic/popular/referrers" % repo)] = [{"referrer": "google.com", "count": 7}]
        class WeeklyWatcher(ow.Watcher):
            def listings(self):
                return {"owner/list#1": {"kind": "prs", "state": "OPEN", "comments": [], "reviews": []},
                        "owner/list#2": {"kind": "prs", "state": "CLOSED", "comments": [], "reviews": []}}
        watcher = WeeklyWatcher(FakeGh(responses), self.state, self.messages.append,
                                repositories=REPOSITORIES)
        self.assertTrue(watcher.weekly())
        self.assertIn("📊 Weekly OSS summary", self.messages[0])
        self.assertIn("views 42", self.messages[0])
        self.assertIn("referrers: google.com(7)", self.messages[0])
        self.assertIn("Tracked PRs: open 1 · merged/closed 1", self.messages[0])

    def test_listings_search_configured_author_and_fetch_missing_tracked_items(self):
        fields = "number,url,title,body,state,repository,closedAt"
        responses = {
            ("search", "prs", "--author", "someone", "--limit", "100", "--json", fields): [
                {"number": 5, "title": "Add one", "repository": {"nameWithOwner": "x/y"}},
                {"number": 6, "title": "unrelated", "repository": {"nameWithOwner": "x/z"}}],
            ("search", "issues", "--author", "someone", "--limit", "100", "--json", fields): [],
            ("api", "repos/upstream/list/issues/2555"): {"number": 2555, "state": "open", "pull_request": {}},
            ("api", "repos/x/y/issues/5/comments"): [{"id": 1}],
            ("api", "repos/x/y/pulls/5/reviews"): [],
            ("api", "repos/upstream/list/issues/2555/comments"): [],
            ("api", "repos/upstream/list/pulls/2555/reviews"): [{"id": 9}],
        }
        watcher = ow.Watcher(FakeGh(responses), self.state, self.messages.append, author="someone",
                             keywords=["one"], tracked=TRACKED)
        self.assertEqual({"x/y#5", "upstream/list#2555"}, set(watcher.listings()))

    def test_main_requires_an_oss_watch_section(self):
        config = Path(self.temp.name) / "config.json"
        config.write_text(json.dumps({"owner_chat_id": 1}))
        config.chmod(0o600)
        self.assertEqual(1, ow.main(["--config", str(config), "--state-dir", self.temp.name, "check"]))


class GhTests(unittest.TestCase):
    def test_gh_parses_fake_subprocess_output_without_a_shell(self):
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, '{"ok": true}', "")
        self.assertEqual({"ok": True}, ow.Gh(runner).json(["api", "repos/example/project"]))
        self.assertEqual(["gh", "api", "repos/example/project"], calls[0])


class LaunchdTests(unittest.TestCase):
    def test_agent_templates_are_valid_private_user_agents(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("fm-telegram.plist", "oss-watch.plist", "oss-weekly.plist"):
            raw = (root / "launchd" / name).read_text()
            for placeholder in ("@LABEL@", "@PYTHON@", "@DATA_DIR@", "@LOG_DIR@"):
                raw = raw.replace(placeholder, "/x")
            config = plistlib.loads(raw.encode())
            self.assertEqual(0o077, config["Umask"])
            self.assertIn("/opt/homebrew/bin", config["EnvironmentVariables"]["PATH"])
            self.assertNotIn("UserName", config)


if __name__ == "__main__":
    unittest.main()
