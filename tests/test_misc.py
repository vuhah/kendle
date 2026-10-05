"""kendle task, kendle disk, and the command line itself."""
import os, unittest
from helpers import KendleTest, Workspace, read


class Task(KendleTest):
    def test_without_a_tracker_it_says_how_to_add_one(self):
        r = self.w.kendle("task", "ABC-1", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn('fetch = "<command> {id}"', r.stderr)

    def test_runs_the_command_with_each_word_quoted(self):
        before = read(os.path.join(self.w.ws, "kendle.toml"))
        self.w.add_toml('[task]\nfetch = "printf \'%s|\' {id}"\n')
        try:
            self.assertEqual(self.w.kendle("task", "proj", "it's 42; rm -rf /").stdout, "proj|it's 42; rm -rf /|")
        finally:
            self.w.write_toml(before)


class Disk(KendleTest):
    TOML = """
        [disk]
        caches = ["dist"]
        budget_gb = 7
    """

    def test_report_trim_and_budget(self):
        self.w.kendle("new", "big")
        dist = os.path.join(self.w.ws, "big", "dist")
        os.makedirs(dist)
        with open(os.path.join(dist, "bundle.js"), "w") as f:
            f.write("x" * 1024 * 1024)
        with open(os.path.join(self.w.ws, "big", "wip.py"), "w") as f:
            f.write("uncommitted")
        out = self.w.kendle("disk").stdout
        self.assertIn("of its 7G budget", out)
        self.assertNotIn("bazel", out)                         # bazel is off by default
        out = self.w.kendle("disk", "trim", "big").stdout
        self.assertIn("(2 uncommitted files kept)", out)       # dist/ and wip.py
        self.assertFalse(os.path.exists(dist))
        self.assertTrue(os.path.exists(os.path.join(self.w.ws, "big", "wip.py")))
        self.assertIn("workspace budget: 9 GB", self.w.kendle("disk", "budget", "9").stdout)
        self.assertIn(" big ", self.w.kendle("disk", "merged").stdout + " ")


class CommandLine(KendleTest):
    def test_help_and_unknown_commands(self):
        out = self.w.kendle("help").stdout
        for cmd in ("kendle init", "kendle stack", "kendle gate", "kendle task", "kendle disk"):
            self.assertIn(cmd, out)
        r = self.w.kendle("bogus", check=False)
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown command 'bogus'", r.stderr)
        for cmd in ("agent", "stack", "gate", "disk", "task", "services", "init", "view"):
            self.assertEqual(self.w.kendle("help", cmd, check=False).returncode in (0, 2), True, cmd)
            self.assertIn("kendle", self.w.kendle("help", cmd, check=False).stdout + self.w.kendle("help", cmd, check=False).stderr)


if __name__ == "__main__":
    unittest.main()
