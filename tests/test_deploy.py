"""`deploy/install.sh --python` and `deploy/launchd-cutover.sh` against fake system commands."""
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABEL = "io.example.fm-telegram"

# Each fake records its arguments in $FAKE/calls; $FAKE holds the state they read.
FAKES = {
    "launchctl": """
case "$1" in
  print) [ -f "$FAKE/loaded" ] || exit 113; printf '\\tpid = %s\\n' "$(cat "$FAKE/loaded")" ;;
  bootout) rm -f "$FAKE/loaded" ;;
  bootstrap) echo 4242 > "$FAKE/loaded" ;;
esac""",
    # -f lists the pollers; -P <shell> -x sleep finds the loop's sleep (none here).
    "pgrep": 'case "$1" in -f) cat "$FAKE/pollers" 2>/dev/null ;; esac; exit 0',
    "ps": 'case "$2" in ppid=) echo 100 ;; comm=) echo -zsh ;; esac',
    "codesign": '[ -f "$FAKE/unsigned" ] && echo "designated => identifier x" || echo \'designated => cdhash H"00"\'',
    "herdr": 'case "$2" in run) echo 777 > "$FAKE/pollers" ;; esac',
    "sleep": ":",
    "fm-telegram-python": "exit 0",  # the dedicated runtime; it lives outside PATH
}


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fake = self.root / "fake"
        bin_dir = self.fake / "bin"
        bin_dir.mkdir(parents=True)
        for name, body in FAKES.items():
            path = (self.fake if name == "fm-telegram-python" else bin_dir) / name
            path.write_text('#!/bin/sh\necho "%s $*" >> "$FAKE/calls"\n%s\n' % (name, body))
            path.chmod(0o755)
        self.data = self.root / "data"
        self.plist = self.root / "Library/LaunchAgents" / (LABEL + ".plist")
        self.env = dict(os.environ, HOME=str(self.root), FAKE=str(self.fake),
                        PATH="%s:%s" % (bin_dir, os.environ["PATH"]), FM_TELEGRAM_LABEL=LABEL,
                        FM_TELEGRAM_DATA_DIR=str(self.data), FM_TELEGRAM_PYTHON=str(self.fake / "fm-telegram-python"),
                        FM_TELEGRAM_STATE_DIR=str(self.root / "state"))

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, *args, **env):
        return subprocess.run([str(ROOT / "deploy" / args[0]), *args[1:]], env=dict(self.env, **env),
                              text=True, capture_output=True, timeout=60)

    def calls(self, name):
        path = self.fake / "calls"
        lines = path.read_text().splitlines() if path.exists() else []
        return [line.split(" ", 1)[1] for line in lines if line.split(" ", 1)[0] == name]

    def state(self, **files):
        for name, text in files.items():
            (self.fake / name).write_text(text)

    def test_install_python_must_be_an_absolute_executable(self):
        script = self.root / "not-executable"
        script.write_text("")
        for python in ["python3", str(self.root / "missing"), str(script), str(self.root)]:
            with self.subTest(python=python):
                result = self.run_script("install.sh", "--launchd", "--python", python)
                self.assertEqual(2, result.returncode, result.stderr)
                self.assertIn("--python must be an absolute path", result.stderr)
                self.assertFalse(self.data.exists())  # rejected before anything was installed
                self.assertEqual([], self.calls("launchctl"))

    def test_install_renders_the_dedicated_interpreter_into_the_plist(self):
        python = self.root / "runtime & co" / "fm-telegram-python"
        python.parent.mkdir()
        python.write_text("#!/bin/sh\n")
        python.chmod(0o755)
        result = self.run_script("install.sh", "--version", "v1", "--launchd", "--python", str(python))
        self.assertEqual(0, result.returncode, result.stderr)
        agent = plistlib.loads(self.plist.read_bytes())  # also proves "&" was escaped
        self.assertEqual([str(python), "%s/current/fm_telegram.py" % self.data, "run"],
                         agent["ProgramArguments"])
        self.assertEqual("Standard", agent["ProcessType"])
        self.assertEqual(["bootout gui/%d/%s" % (os.getuid(), LABEL),
                          "bootstrap gui/%d %s" % (os.getuid(), self.plist)], self.calls("launchctl"))

    def test_check_passes_with_one_poller_under_a_shell(self):
        self.state(pollers="4242\n")
        result = self.run_script("launchd-cutover.sh", "check")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("one poller: pid 4242 under -zsh", result.stdout)
        self.assertIn("rollback must restart the loop by hand", result.stdout)
        self.assertIn("check passed", result.stdout)
        self.assertIn("-I -c import ssl", self.calls("fm-telegram-python"))

    def test_check_fails_on_each_unsafe_condition(self):
        cases = {
            "more than one poller": ({"pollers": "1\n2\n"}, {}),
            "is already loaded": ({"loaded": "4242"}, {}),
            "not ad-hoc signed": ({"unsigned": ""}, {}),
            "set FM_TELEGRAM_PYTHON": ({}, {"FM_TELEGRAM_PYTHON": ""}),
        }
        for message, (files, env) in cases.items():
            with self.subTest(message):
                for name in ["pollers", "loaded", "unsigned"]:
                    (self.fake / name).unlink(missing_ok=True)
                self.state(**files)
                result = self.run_script("launchd-cutover.sh", "check", **env)
                self.assertEqual(1, result.returncode)
                self.assertIn(message, result.stdout)
                self.assertIn("check FAILED", result.stdout)

    def test_rollback_parks_the_plist_and_restarts_the_pane_loop(self):
        self.plist.parent.mkdir(parents=True)
        self.plist.write_text("agent")
        self.state(loaded="4242")
        result = self.run_script("launchd-cutover.sh", "rollback",
                                 FM_TELEGRAM_PANE="w1:p1", FM_TELEGRAM_LOOP="while true; do x; done")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("bootout gui/%d/%s" % (os.getuid(), LABEL), self.calls("launchctl"))
        self.assertFalse(self.plist.exists())
        self.assertEqual("agent", (self.data / (LABEL + ".plist.rolled-back")).read_text())
        self.assertIn("pane run w1:p1 while true; do x; done", self.calls("herdr"))
        self.assertIn("pane loop restarted in w1:p1: pid 777", result.stdout)

    def test_rollback_never_starts_a_second_poller(self):
        self.state(pollers="4242\n")  # still polling after bootout
        result = self.run_script("launchd-cutover.sh", "rollback",
                                 FM_TELEGRAM_PANE="w1:p1", FM_TELEGRAM_LOOP="loop")
        self.assertEqual(1, result.returncode)
        self.assertIn("poller still running after bootout", result.stderr)
        self.assertNotIn("pane run", " ".join(self.calls("herdr")))

    def test_rollback_without_a_pane_prints_the_manual_step(self):
        result = self.run_script("launchd-cutover.sh", "rollback", FM_TELEGRAM_LOOP="my loop")
        self.assertEqual(1, result.returncode)
        self.assertIn("restart the previous loop by hand:\n  my loop", result.stdout)
        self.assertEqual([], self.calls("herdr"))


if __name__ == "__main__":
    unittest.main()
