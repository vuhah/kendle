"""kendle gate: git checks, steps in lanes, the pass cache, and background runs."""
import os, sys, time, unittest
from helpers import KendleTest, read

MESSAGE_CHECK = (f"{sys.executable} -c \"import sys; t = open(sys.argv[1]).readline(); "
                 f"sys.exit(0 if ' - ' in t else print('title must read <Area> - <what>') or 1)\" {{file}}")
MESSAGE_TOML = MESSAGE_CHECK.replace('"', '\\"')


class Gate(KendleTest):
    TOML = f"""
        [gate]
        max_commits = 1
        stray = ["^\\\\.idea/", "\\\\.orig$"]
        message = "{MESSAGE_TOML}"
        cleanup = ["targets.txt"]

        [gate.steps.unit]
        run = "echo unit in {{worktree}}; echo t > targets.txt; cat $KENDLE_CHANGED"

        [gate.steps.scope]
        run = "echo '::warn:: coverage: 1 of 3 tests cover the change'"
        lane = "b"

        [gate.steps.web]
        run = "echo web"
        when = ["web/", "*.ts"]

        [gate.steps.build]
        run = "echo building; exit 3"
        lane = "c"
        full = true

        [gate.steps.after-build]
        run = "echo after"
        lane = "c"
        full = true
        needs = ["build"]

        [gate.steps.tool]
        run = "echo 'exec format error'; exit 1"
        lane = "d"
        warn_if = "(?i)exec format error"
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.w.kendle("new", "ship")
        cls.path = os.path.join(cls.w.ws, "ship")
        cls.w.commit(cls.path, {"login.py": "x = 1\n"}, "Login - faster query")

    def gate(self, *args):
        r = self.w.kendle("gate", *args, cwd=self.path, check=False)
        return r.returncode, r.stdout

    def rows(self, out):
        return {l[8:].split(" (")[0]: l[2:6].strip() for l in out.splitlines() if l[2:6].strip() in ("PASS", "FAIL", "WARN", "SKIP")}

    def test_1_steps_lanes_filters_and_rows(self):
        code, out = self.gate()
        self.assertEqual(code, 0, out)
        rows = self.rows(out)
        for name in ("fetched origin/main", "on top of origin/main", "exactly one commit ahead",
                     "clean tree - everything is in the commit", "no stray files in the change",
                     "commit message follows the template", "unit"):
            self.assertEqual(rows.get(name), "PASS", (name, out))
        self.assertEqual((rows["coverage"], rows["scope"], rows["tool"]), ("WARN", "PASS", "WARN"))
        self.assertIn("1 of 3 tests cover the change", out)
        self.assertNotIn("web", rows)                          # nothing under web/ or *.ts changed
        self.assertNotIn("build", rows)                        # --full only
        self.assertIn("login.py", read(os.path.join(self.w.git(self.path, "rev-parse", "--absolute-git-dir"),
                                                    "kendle-gate", "unit.log")))
        self.assertFalse(os.path.exists(os.path.join(self.path, "targets.txt")))   # cleaned up
        self.assertIn("GATE PASSED", out)

    def test_2_a_pass_is_not_run_again(self):
        code, out = self.gate()
        self.assertEqual(code, 0)
        self.assertIn("nothing changed since they last passed", out)
        self.assertNotIn("....  unit", out)

    def test_3_full_runs_more_and_needs_skip(self):
        code, out = self.gate("--full")
        self.assertEqual(code, 1)
        rows = self.rows(out)
        self.assertEqual((rows["build"], rows["after-build"]), ("FAIL", "SKIP"))
        self.assertIn("needs build to pass", out)
        self.assertIn("GATE FAILED - do not push", out)

    def test_4_git_checks_stop_the_gate(self):
        with open(os.path.join(self.path, "stray.orig"), "w") as f:
            f.write("x")
        code, out = self.gate()
        os.remove(os.path.join(self.path, "stray.orig"))
        self.assertEqual(code, 1)
        self.assertEqual(self.rows(out)["clean tree - everything is in the commit"], "FAIL")
        self.assertIn("the steps did not run", out)
        self.w.commit(self.path, {"two.py": "y\n"}, "Login - second commit")
        code, out = self.gate()
        self.assertEqual(self.rows(out)["exactly one commit ahead"], "FAIL")
        self.w.git(self.path, "reset", "-q", "--hard", "HEAD^")

    def test_5_message_check(self):
        bad = os.path.join(self.w.root, "msg.txt")
        with open(bad, "w") as f:
            f.write("fixed stuff\n")
        r = self.w.kendle("gate", "--message", bad, cwd=self.path, check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("title must read", r.stdout)
        self.assertEqual(self.w.kendle("gate", "--message", "HEAD", cwd=self.path).returncode, 0)

    def test_6_background_start_and_wait(self):
        self.w.commit(self.path, {"login.py": "x = 2\n"}, "Login - faster query")
        self.w.git(self.path, "reset", "-q", "--soft", "HEAD~2")
        self.w.git(self.path, "commit", "-q", "-m", "Login - faster query, again")
        self.assertIn("gate started in the background", self.w.kendle("gate", "--start", cwd=self.path).stdout)
        r = self.w.kendle("gate", "--wait", cwd=self.path, check=False)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("GATE PASSED", r.stdout)


if __name__ == "__main__":
    unittest.main()
