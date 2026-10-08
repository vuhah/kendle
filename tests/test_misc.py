"""kendle task, kendle disk, the sidebar's key map, and the command line itself."""
import os, shutil, sys, tempfile, textwrap, time, unittest
from helpers import FAKEBIN, ROOT, KendleTest, Workspace, free_port, read, sh


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


class DeskAndReviewDisk(KendleTest):
    TOML = f"""
        [disk]
        caches = ["dist"]

        [services.web]
        run = "exec {sys.executable} {os.path.join(FAKEBIN, 'listen')} {{port}}"
        port = {free_port()}
    """

    def tearDown(self):
        self.w.kendle("stack", "stop", "-f", "ask", check=False)

    def test_the_desk_and_reviews_are_counted_and_trimmed_only_while_no_stack_runs(self):
        desk = os.path.join(self.w.ws, "ask")
        review = os.path.join(self.w.ws, "review-1")
        self.w.git(self.w.app, "worktree", "add", "-q", "--detach", review, "origin/main")
        dist = os.path.join(desk, "dist")
        os.makedirs(dist)
        with open(os.path.join(dist, "bundle.js"), "w") as f:
            f.write("x" * 1024 * 1024)
        out = self.w.kendle("disk").stdout
        section = out.split("\n  desk and reviews\n")[1]
        self.assertIn("    ask ", section)
        self.assertIn("    review-1 ", section)
        self.w.kendle("stack", "start", "web", cwd=desk)
        self.assertEqual(self.w.kendle("stack", "wait", cwd=desk).returncode, 0)
        self.assertIn("skipped ask: it is working right now or its services are up", self.w.kendle("disk", "trim", "ask").stdout)
        self.assertTrue(os.path.exists(dist))
        self.w.kendle("stack", "stop", cwd=desk)
        self.assertIn("  dist", self.w.kendle("disk", "trim", "ask").stdout)
        self.assertFalse(os.path.exists(dist))
        self.assertIn("no feature, Ask desk or review folder 'nope'", self.w.kendle("disk", "trim", "nope").stdout)

    def test_trim_and_idle_print_nothing_and_return_their_lines(self):
        r = self.w.py(textwrap.dedent("""
            import io, contextlib
            from kendle.cmd import disk
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                trimmed = disk.trim(["nope"])
                idled = disk.idle(10000)
            print(json.dumps([out.getvalue(), trimmed, idled]))
        """))
        self.assertEqual(r[0], "")
        self.assertEqual(r[1][0], 0.0)
        self.assertIn("  no feature, Ask desk or review folder 'nope'", r[1][1])
        self.assertEqual(r[2], [0.0, ["  nothing has been idle for 10000 days"]])


    def test_the_consoles_disk_watch_trims_a_quiet_review_never_a_running_desk_and_prints_nothing(self):
        desk = os.path.join(self.w.ws, "ask")
        review = os.path.join(self.w.ws, "review-2")
        self.w.git(self.w.app, "worktree", "add", "-q", "--detach", review, "origin/main")
        self.addCleanup(self.w.git, self.w.app, "worktree", "remove", "--force", review)
        self.addCleanup(shutil.rmtree, os.path.join(desk, "dist"), True)
        for folder in (desk, review):
            os.makedirs(os.path.join(folder, "dist"), exist_ok=True)
            with open(os.path.join(folder, "dist", "bundle.js"), "w") as f:
                f.write("x" * 1024)
        self.w.kendle("stack", "start", "web", cwd=desk)
        self.assertEqual(self.w.kendle("stack", "wait", cwd=desk).returncode, 0)
        try:
            r = self.w.py(textwrap.dedent("""
                import io, os, contextlib, types
                from kendle import core
                from kendle.cmd import disk
                from kendle.cmd.sidebar import Sidebar
                out, err, said = io.StringIO(), io.StringIO(), []
                stand_in = types.SimpleNamespace(say=lambda text, error=False: said.append([text, error]))
                core.free_gb = lambda: 100
                disk.orphan_bases = lambda: []
                disk.gb = lambda path: 10.0
                disk.budget = lambda new=None: 5                # over budget
                disk.last_touch = lambda path: 0                # every folder quiet for ages
                os.environ.pop("KENDLE_STATE")
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    Sidebar.watch_disk(stand_in)
                    lines = disk.enforce(dry=True)[1]
                print(json.dumps([out.getvalue(), err.getvalue(), said, lines]))
            """))
        finally:
            self.w.kendle("stack", "stop", "-f", "ask", check=False)
        out, err, said, _ = r
        self.assertEqual([out, err], ["", ""])
        self.assertEqual(len(said), 1, said)
        self.assertTrue(said[0][0].startswith("hub was"), said)
        self.assertFalse(said[0][1])
        self.assertTrue(os.path.exists(os.path.join(desk, "dist")))          # its stack runs
        self.assertFalse(os.path.exists(os.path.join(review, "dist")))          # quiet, and nothing runs there

class Sidebar(unittest.TestCase):
    """The curses sidebar is not driven by a test; S runs `kendle stack` from the workspace folder,
    which the stack tests cover. Here: S is mapped, and the help tells it apart from s."""

    def test_s_and_S_are_listed_together_and_S_is_mapped(self):
        source = read(os.path.join(ROOT, "kendle", "cmd", "sidebar.py"))
        self.assertIn('"S": self.stack_toggle', source.split("actions = {", 1)[1].split("}", 1)[0])
        doc = source.split('"""', 2)[1]
        self.assertIn("s read-only sub-agent · S start/stop the desk's or a review's services", doc)

    def test_S_on_the_desk_says_its_stack_stops_when_the_desk_moves_and_when_it_closes(self):
        source = read(os.path.join(ROOT, "kendle", "cmd", "sidebar.py"))
        start = source.split("def stack_toggle", 1)[1].split("\n    def ", 1)[0]
        self.assertIn('" - they stop when the next question moves the desk,"\n', start)
        self.assertIn('" and when it closes" if name == _stack.desk_name() else ""', start)
        doc = source.split('"""', 2)[1]
        self.assertIn("stops when the next question moves the desk, and when it closes", doc)


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
