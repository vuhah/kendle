"""kendle task, kendle disk, and the command line itself."""
import os, tempfile, textwrap, time, unittest
from helpers import KendleTest, Workspace, read, sh


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

    # The console's background disk check runs these routines while curses owns the screen: they
    # must print nothing. Each snippet captures stdout/stderr around the call and returns them.
    CAPTURE = """
        import io, os, contextlib, types
        from kendle import core
        from kendle.cmd import disk
        out, err, said = io.StringIO(), io.StringIO(), []
        stand_in = types.SimpleNamespace(say=lambda text, error=False: said.append([text, error]))
    """

    def snippet(self, *parts):
        return self.w.py("".join(textwrap.dedent(p) for p in (self.CAPTURE,) + parts))

    def quiet_feature(self, name):
        """A feature with a 1M dist/ cache and a last commit two days old."""
        self.w.kendle("new", name)
        path = os.path.join(self.w.ws, name)
        os.makedirs(os.path.join(path, "dist"))
        with open(os.path.join(path, "dist", "bundle.js"), "w") as f:
            f.write("x" * 1024 * 1024)
        sh("git", "-C", path, "-c", "user.email=t@example.com", "-c", "user.name=Tester",
           "commit", "-q", "--allow-empty", "-m", "old work",
           env={**os.environ, "GIT_COMMITTER_DATE": f"{int(time.time()) - 2 * 86400} +0000"})
        return path

    def test_enforce_prints_nothing_and_returns_its_report(self):
        path = self.quiet_feature("stale")
        r = self.snippet("""
            disk.gb = lambda path: 10.0                         # over any budget; every cache worth trimming
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                freed, lines = disk.enforce()
            print(json.dumps([out.getvalue(), err.getvalue(), freed, lines]))
        """)
        self.assertEqual(r[:2], ["", ""])
        self.assertGreater(r[2], 0)
        self.assertTrue(any("stale" in line for line in r[3]), r[3])
        self.assertFalse(os.path.exists(os.path.join(path, "dist")))

    def watch_disk(self, stubs):
        """Sidebar.watch_disk on a stand-in that records say(), outside the test console's guard."""
        return self.snippet(stubs, """
            from kendle.cmd.sidebar import Sidebar
            os.environ.pop("KENDLE_STATE")
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                Sidebar.watch_disk(stand_in)
            print(json.dumps([out.getvalue(), err.getvalue(), said]))
        """)

    def test_console_over_budget_trims_quietly(self):
        path = self.quiet_feature("dusty")
        out, err, said = self.watch_disk("""
            core.free_gb = lambda: 100
            disk.orphan_bases = lambda: []
            disk.gb = lambda path: 10.0
        """)
        self.assertEqual([out, err], ["", ""])
        self.assertEqual(len(said), 1, said)
        self.assertTrue(said[0][0].startswith("hub was"), said)
        self.assertFalse(said[0][1])
        self.assertFalse(os.path.exists(os.path.join(path, "dist")))

    def test_console_deletes_orphans_quietly(self):
        base = tempfile.mkdtemp(dir=self.w.root)
        out, err, said = self.watch_disk(f"""
            core.free_gb = lambda: 22
            disk.orphan_bases = lambda: [({base!r}, "/gone")]
            disk.footprint = lambda: 0.0
        """)
        self.assertEqual([out, err], ["", ""])
        self.assertEqual(len(said), 1, said)
        self.assertTrue(said[0][0].startswith("disk 22G - deleted"), said)
        self.assertFalse(said[0][1])
        self.assertFalse(os.path.exists(base))

    def test_console_reports_a_failed_check(self):
        out, err, said = self.watch_disk("""
            core.free_gb = lambda: 100
            disk.orphan_bases = lambda: []
            def broken():
                raise OSError("du went away")
            disk.footprint = broken
        """)
        self.assertEqual([out, err], ["", ""])
        self.assertEqual(said, [["disk check failed: du went away", True]])


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
