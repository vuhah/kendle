"""kendle stack / kendle services: real processes on free ports, stacks side by side - features, the
Ask desk and review folders."""
import json, os, shutil, socket, subprocess, sys, time, unittest
from helpers import FAKEBIN, KendleTest, free_port, read, wait_for


def answers(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


WEB, DB = free_port(), free_port()
LISTEN = os.path.join(FAKEBIN, "listen")


class Stack(KendleTest):
    TOML = f"""
        [services]
        app = "web"

        [services.web]
        run = "exec {sys.executable} {LISTEN} {{port}}"
        port = {WEB}
        watch = ["web/"]
        watch_names = ["server.py"]

        [services.db]
        port = {DB}
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for name in ("one", "two"):
            cls.w.kendle("new", name)

    def tearDown(self):
        for name in ("one", "two"):
            self.w.kendle("stack", "stop", "-f", name, check=False)

    def status(self, feature):
        return {line.split()[1]: line.split()[0] for line in
                self.w.kendle("stack", "status", "-f", feature).stdout.splitlines() if line.startswith("  ") and "port" in line}

    def test_two_features_run_side_by_side_on_shifted_ports(self):
        out = self.w.kendle("stack", "start", "web", "-f", "one").stdout
        self.assertIn(f"starting on port {WEB}", out)
        self.assertIn(f"app at http://localhost:{WEB}", out)
        self.assertEqual(self.w.kendle("stack", "wait", "-f", "one").returncode, 0)
        out = self.w.kendle("stack", "start", "web", "-f", "two").stdout
        self.assertIn(f"starting on port {WEB + 100}", out)
        self.assertIn(f"stack 2 · app at http://127.0.0.1:{WEB + 100}", out)
        self.w.kendle("stack", "wait", "-f", "two")
        self.assertTrue(answers(WEB) and answers(WEB + 100))
        self.assertEqual(self.status("one"), {"web": "up"})
        self.assertIn(f"up    web", self.w.kendle("services").stdout)
        self.assertEqual(self.w.kendle("stack", "stop", "-f", "one").stdout.strip(), "stopped: web")
        self.assertTrue(wait_for(lambda: not answers(WEB)))
        self.assertTrue(answers(WEB + 100))                    # the other stack is untouched
        self.assertIn("stopped by kendle stack", read(os.path.join(self.w.state, "stack", "one", "web.log")))

    def test_a_port_held_by_someone_else_is_refused_and_never_killed(self):
        errors = os.path.join(self.w.root, "other-server.log")
        with open(errors, "w") as log:
            other = subprocess.Popen([sys.executable, LISTEN, str(WEB)],
                                     stdout=log, stderr=subprocess.STDOUT)
        try:
            up = wait_for(lambda: answers(WEB) or other.poll() is not None, timeout=30)
            self.assertTrue(up and other.poll() is None,
                            f"the stand-in server did not start (exit {other.poll()}): {read(errors)[-800:]}")
            r = self.w.kendle("stack", "start", "web", "-f", "one", check=False)
            self.assertEqual(r.returncode, 1)
            self.assertIn(f"port {WEB} for web is held by pid {other.pid}", r.stderr)
            self.w.kendle("stack", "stop", "-f", "one")
            self.assertIsNone(other.poll())                    # still running
        finally:
            other.kill()
            other.wait()

    def test_a_crash_is_reported_as_crashed(self):
        self.w.add_toml(f'[services.broken]\nrun = "echo boom; exit 3"\nport = {free_port()}\n')
        try:
            self.w.kendle("stack", "start", "broken", "-f", "one")
            r = self.w.kendle("stack", "wait", "broken", "-f", "one", check=False)
            self.assertEqual(r.returncode, 1)
            self.assertIn("-> crashed", r.stdout)
            self.assertIn("boom", self.w.kendle("stack", "logs", "broken", "-f", "one").stdout)
        finally:
            self.w.write_toml(read(os.path.join(self.w.ws, "kendle.toml")).split("[services.broken]")[0])

    def test_wait_on_a_service_never_started_returns_at_once(self):
        began = time.time()
        r = self.w.kendle("stack", "wait", "web", "-f", "two", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("-> not started", r.stdout)
        self.assertLess(time.time() - began, 5)
        self.w.kendle("stack", "start", "web", "-f", "one")
        r = self.w.kendle("stack", "wait", "web", "-f", "two", check=False)   # another feature's start is not this one's
        self.assertIn("-> not started", r.stdout)

    def test_check_only_services_are_never_started_or_shifted(self):
        r = self.w.kendle("stack", "start", "db", "-f", "one", check=False)
        self.assertIn("db is check-only", r.stderr)
        line = next(l for l in self.w.kendle("services").stdout.splitlines() if " db " in l)
        self.assertIn(f"port {DB}", line)
        self.assertNotIn(str(DB + 100), line)                  # a shared database stays where it is
        self.assertEqual(self.w.kendle("services", "db", check=False).returncode, 1)

    def test_restart_affected_only_for_watched_files(self):
        feature = os.path.join(self.w.ws, "one")
        self.w.kendle("stack", "start", "web", "-f", "one")
        self.w.kendle("stack", "wait", "-f", "one")
        os.makedirs(os.path.join(feature, "web"), exist_ok=True)
        with open(os.path.join(feature, "web", "notes.md"), "w") as f:
            f.write("x")
        self.w.git(feature, "add", "-A")
        self.assertIn("nothing to restart", self.w.kendle("stack", "restart-affected", cwd=feature).stdout)
        with open(os.path.join(feature, "web", "server.py"), "w") as f:
            f.write("x")
        self.w.git(feature, "add", "-A")
        self.assertIn("restarting web: web/server.py", self.w.kendle("stack", "restart-affected", cwd=feature).stdout)
        self.w.git(feature, "reset", "-q", "--hard")

    def test_stop_idle_spares_busy_features(self):
        self.w.kendle("stack", "start", "web", "-f", "one")
        self.w.kendle("stack", "wait", "-f", "one")
        self.assertIn("none", self.w.kendle("stack", "stop-idle", "--minutes", "60").stdout)
        time.sleep(1.1)
        got = self.w.py("""
            from kendle import stack
            print(json.dumps(stack.stop_idle(minutes=1 / 60)))""")
        self.assertEqual(got, ["one"])


RO = free_port()


class ReadOnlyFolders(KendleTest):
    """The Ask desk and a review folder own stacks too: the last slot is kept for them."""
    TOML = f"""
        [services]
        app = "web"

        [services.web]
        run = "exec {sys.executable} {LISTEN} {{port}}"
        port = {RO}
    """
    NAMES = ("one", "two", "three", "ask", "review-1")

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for name in ("one", "two", "three"):
            cls.w.kendle("new", name)
        cls.desk = os.path.join(cls.w.ws, "ask")
        cls.review = os.path.join(cls.w.ws, "review-1")
        cls.w.git(cls.w.app, "worktree", "add", "-q", "--detach", cls.review, "origin/main")

    def tearDown(self):
        for name in self.NAMES:
            self.w.kendle("stack", "stop", "-f", name, check=False)

    def state(self, name):
        rows = [l.split() for l in self.w.kendle("stack", "status", "-f", name).stdout.splitlines()
                if l.startswith("  ") and " port " in l]
        return rows[0][0] if rows else None

    def test_the_desk_takes_the_kept_stack_with_ports_moved_twice_and_shares_logins(self):
        self.w.kendle("stack", "start", "web", "-f", "one")
        out = self.w.kendle("stack", "start", "web", "-f", "two").stdout
        self.assertNotIn("shares logins", out)                 # stacks 1 and 2 are as they always were
        out = self.w.kendle("stack", "start", "web", cwd=self.desk).stdout
        self.assertIn(f"starting on port {RO + 200}", out)
        self.assertIn(f"stack 3 · app at http://127.0.0.1:{RO + 200}", out)
        self.assertIn("shares logins with stack 2 (same host)", out)
        self.assertEqual(self.w.kendle("stack", "wait", cwd=self.desk).returncode, 0)
        self.assertTrue(answers(RO + 200))
        out = self.w.kendle("stack", "status", cwd=self.desk).stdout
        self.assertIn("ask  (desk · stack 3", out)
        self.assertIn("shares logins with stack 2 (same host)", out)
        self.assertIn("one  (feature · stack 1", self.w.kendle("stack", "status", "-f", "one").stdout)

    def test_features_never_take_the_last_stack_and_replace_never_stops_the_desk(self):
        self.w.kendle("stack", "start", "web", "-f", "one")
        time.sleep(0.05)
        self.w.kendle("stack", "start", "web", "-f", "two")
        r = self.w.kendle("stack", "start", "web", "-f", "three", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("2 feature stacks are already running (one (feature), two (feature))", r.stderr)
        self.w.kendle("stack", "start", "web", cwd=self.desk)
        r = self.w.kendle("stack", "start", "web", "-f", "three", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("all 3 stacks are running", r.stderr)    # every stack taken: says so, not the reserve
        self.assertNotIn("kept for the Ask desk", r.stderr)
        r = self.w.kendle("stack", "start", "web", cwd=self.review, check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("all 3 stacks are running", r.stderr)
        self.assertIn("ask (desk)", r.stderr)
        self.assertIn(f"starting on port {RO}", self.w.kendle("stack", "start", "web", "-f", "three", "--replace").stdout)
        self.assertEqual(self.state("one"), "stopped")         # the oldest feature made room
        self.assertIn(self.state("ask"), ("up", "starting"))   # never the desk
        self.assertIn(self.state("two"), ("up", "starting"))

    def test_a_review_folder_runs_only_its_own_stack(self):
        self.w.kendle("stack", "start", "web", "-f", "one")
        self.w.kendle("stack", "wait", "-f", "one")
        out = self.w.kendle("stack", "start", "web", cwd=self.review).stdout
        self.assertIn(f"starting on port {RO + 100}", out)
        self.assertEqual(self.w.kendle("stack", "wait", cwd=self.review).returncode, 0)
        for cmd in (["stop"], ["start", "web"], ["restart", "web"], ["restart-affected"], ["wait"]):
            r = self.w.kendle("stack", *cmd, "-f", "one", cwd=self.review, check=False)
            self.assertEqual(r.returncode, 1, cmd)
            self.assertIn("a read-only folder runs only its own stack (review-1)", r.stderr)
        self.assertEqual(self.state("one"), "up")
        self.assertIn("one  (feature", self.w.kendle("stack", "status", "-f", "one", cwd=self.review).stdout)
        self.assertIn("start web in one", self.w.kendle("stack", "logs", "web", "-f", "one", cwd=self.review).stdout)
        self.assertIn("review-1  (review", self.w.kendle("stack", "status", "-f", "review-1", cwd=self.review).stdout)
        self.assertEqual(self.w.kendle("stack", "stop", "--why", "done here", cwd=self.review).stdout.strip(), "stopped: web")
        self.assertIn("stopped by kendle stack - done here", read(os.path.join(self.w.state, "stack", "review-1", "web.log")))
        r = self.w.kendle("stack", "status", cwd=self.w.ws, check=False)
        self.assertIn("inside a feature, the Ask desk or a review folder, or pass -f <name>", r.stderr)

    def test_idle_stop_spares_a_desk_stack_whose_question_is_working(self):
        from helpers import user, assistant
        qid = self.w.kendle("ask", "does the login page load?").stdout.split()[1]
        try:
            path = self.w.transcript_path(qid, self.desk)
            self.w.write_transcript(path, [user("does it load?"), assistant(stop="tool_use", tool=("Bash", {}))])
            self.w.kendle("stack", "start", "web", cwd=self.desk)
            self.w.kendle("stack", "wait", cwd=self.desk)
            time.sleep(1.1)
            idle = lambda: self.w.py("""
                from kendle import stack
                print(json.dumps(stack.stop_idle(minutes=1 / 60)))""")
            self.assertEqual(idle(), [])                       # the question is mid-turn
            self.w.write_transcript(path, [user("does it load?", ago=60), assistant("Yes", ago=60)])
            self.assertEqual(idle(), ["ask"])
        finally:
            self.w.kendle("stop", qid)

    def test_a_question_runs_the_app_on_stack_3_and_closing_it_leaves_the_features_alone(self):
        self.w.kendle("stack", "start", "web", "-f", "one")
        self.w.kendle("stack", "start", "web", "-f", "two")
        qid = self.w.kendle("ask", "does the login screen show an error on a wrong password?").stdout.split()[1]
        out = self.w.kendle("stack", "start", "web", "-f", "ask", cwd=self.desk).stdout   # its own name: allowed
        self.assertIn(f"stack 3 · app at http://127.0.0.1:{RO + 200}", out)
        self.assertEqual(self.w.kendle("stack", "wait", cwd=self.desk).returncode, 0)
        began = time.time()
        self.w.kendle("agent", "stop", qid)
        self.assertLess(time.time() - began, 5)                # the stack stops on its own; nobody waits
        self.assertTrue(wait_for(lambda: self.state("ask") == "stopped", timeout=30), self.state("ask"))
        self.assertEqual(self.state("one"), "up")
        self.assertEqual(self.state("two"), "up")
        self.assertTrue(answers(RO) and answers(RO + 100))

    def test_replace_from_the_desk_or_a_review_stops_nothing(self):
        self.w.kendle("stack", "start", "web", "-f", "one")
        self.w.kendle("stack", "start", "web", cwd=self.desk)
        self.w.kendle("stack", "start", "web", cwd=self.review)
        for name, folder in (("ask", self.desk), ("review-1", self.review)):
            self.w.kendle("stack", "stop", cwd=folder)
            self.w.kendle("stack", "start", "web", "-f", "two")
            r = self.w.kendle("stack", "start", "web", "--replace", cwd=folder, check=False)
            self.assertEqual(r.returncode, 1, name)
            self.assertIn("all 3 stacks are running", r.stderr)
            self.assertEqual([self.state(x) for x in ("one", "two")], ["up", "up"], name)
            self.assertEqual(self.state("review-1" if name == "ask" else "ask"), "up", name)
            self.w.kendle("stack", "stop", "-f", "two")
            self.w.kendle("stack", "start", "web", cwd=folder)

    def test_a_feature_with_replace_stops_a_feature_never_the_desk_or_a_review(self):
        self.w.kendle("stack", "start", "web", cwd=self.desk)
        self.w.kendle("stack", "start", "web", cwd=self.review)
        self.w.kendle("stack", "start", "web", "-f", "one")      # the one slot left
        r = self.w.kendle("stack", "start", "web", "-f", "two", check=False)
        self.assertEqual(r.returncode, 1)
        for held in ("ask (desk)", "review-1 (review)", "one (feature)"):
            self.assertIn(held, r.stderr)
        self.w.kendle("stack", "start", "web", "-f", "two", "--replace")
        self.assertEqual(self.state("one"), "stopped")
        self.assertEqual([self.state(x) for x in ("ask", "review-1")], ["up", "up"])
        self.assertIn(self.state("two"), ("up", "starting"))

    def test_with_two_stacks_and_no_feature_running_replace_stops_nothing(self):
        toml = os.path.join(self.w.ws, "kendle.toml")
        before = read(toml)
        try:
            self.w.write_toml(before.replace("[services]\n", "[services]\nstacks = 2\n"))
            self.w.kendle("stack", "start", "web", cwd=self.desk)
            self.w.kendle("stack", "start", "web", cwd=self.review)
            r = self.w.kendle("stack", "start", "web", "-f", "one", "--replace", check=False)
            self.assertEqual(r.returncode, 1)
            self.assertIn("ask (desk), review-1 (review)", r.stderr)
            self.assertNotIn("pass --replace", r.stderr)          # there is no feature stack to replace
            self.assertEqual([self.state(x) for x in ("ask", "review-1")], ["up", "up"])
        finally:
            self.w.write_toml(before)

    def test_the_refusal_says_all_stacks_are_running_or_why_a_free_one_is_kept(self):
        self.w.kendle("stack", "start", "web", cwd=self.desk)
        self.w.kendle("stack", "start", "web", cwd=self.review)
        self.w.kendle("stack", "start", "web", "-f", "one")
        r = self.w.kendle("stack", "start", "web", "-f", "two", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("all 3 stacks are running (ask (desk), one (feature), review-1 (review)) - stop one first "
                      "(kendle stack stop -f <name>) or pass --replace to stop the oldest feature's", r.stderr)
        self.assertNotIn("kept for the Ask desk", r.stderr)    # the desk and a review hold two: not the reserve's fault
        for name in ("ask", "review-1"):
            self.w.kendle("stack", "stop", "-f", name)
        toml = os.path.join(self.w.ws, "kendle.toml")
        before = read(toml)
        try:
            self.w.write_toml(before.replace("[services]\n", "[services]\nstacks = 2\n"))
            r = self.w.kendle("stack", "start", "web", "-f", "two", check=False)
            self.assertEqual(r.returncode, 1)
            self.assertIn("1 feature stack is already running (one (feature)); the last stack is kept for the Ask "
                          "desk and reviews - stop one first (kendle stack stop -f <name>) or pass --replace to stop "
                          "the oldest feature's", r.stderr)
            self.assertEqual(self.state("one"), "up")
        finally:
            self.w.write_toml(before)
        self.w.kendle("stack", "start", "web", "-f", "two")
        r = self.w.kendle("stack", "start", "web", "-f", "three", check=False)
        self.assertIn("2 feature stacks are already running (one (feature), two (feature)); the last stack is kept",
                      r.stderr)

    def test_a_review_reads_its_own_errors_and_its_stack_stops_by_name_once_the_folder_is_gone(self):
        gone = os.path.join(self.w.ws, "review-2")
        self.w.git(self.w.app, "worktree", "add", "-q", "--detach", gone, "origin/main")
        try:
            self.w.kendle("stack", "start", "web", cwd=gone)
            self.assertEqual(self.w.kendle("stack", "wait", cwd=gone).returncode, 0)
            with open(os.path.join(self.w.state, "stack", "review-2", "web.log"), "a") as log:
                log.write("ERROR: the login form posted nothing\n")
            out = self.w.kendle("stack", "logs", "web", "--errors", cwd=gone).stdout
            self.assertIn("ERROR: the login form posted nothing", out)
            self.w.git(self.w.app, "worktree", "remove", "--force", gone)
            self.assertEqual(self.w.kendle("stack", "stop", "-f", "review-2").stdout.strip(), "stopped: web")
        finally:
            self.w.kendle("stack", "stop", "-f", "review-2", check=False)
            if os.path.isdir(gone):
                self.w.git(self.w.app, "worktree", "remove", "--force", gone)

    def test_stacks_must_be_two_or_more(self):
        toml = os.path.join(self.w.ws, "kendle.toml")
        before = read(toml)
        try:
            for bad in ("1", "true", '"3"'):
                self.w.write_toml(before.replace("[services]\n", f"[services]\nstacks = {bad}\n"))
                r = self.w.kendle("stack", "status", "-f", "one", check=False)
                self.assertNotEqual(r.returncode, 0, bad)
                self.assertIn("kendle.toml: services.stacks must be 2 or more", r.stderr, bad)
        finally:
            self.w.write_toml(before)


CACHE, PG = free_port(), free_port()


class Compose(KendleTest):
    """Compose services through tests/fakebin/docker, which reads the compose file (JSON) as compose
    does - ${VAR:-default} included - and listens on the published port for `up`."""
    TOML = f"""
        [services.cache]
        compose = "cache"

        [services.db]
        compose = "db"
        port = {PG}
    """
    COMPOSE = {"services": {"cache": {"image": "redis:7", "ports": [f"{CACHE}:6379"]},
                            "db": {"image": "postgres:17", "ports": [f"${{KENDLE_PORT:-{PG}}}:5432"]}}}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for name in ("c1", "c2"):
            cls.w.kendle("new", name)
            with open(os.path.join(cls.w.ws, name, "docker-compose.yml"), "w") as f:
                json.dump(cls.COMPOSE, f)

    def tearDown(self):
        for name in ("c1", "c2"):
            self.w.kendle("stack", "stop", "-f", name, check=False)

    def test_compose_service_comes_from_the_compose_file(self):
        got = self.w.py("""
            from kendle import core, services
            s = services.spec("cache", core.feature("c1"), 0)
            print(json.dumps([s["argv"][-4:], s["port"], s["up_when"], s["stop"][-2:]]))""")
        self.assertEqual(got, [["kendle-c1", "up", "--no-log-prefix", "cache"], CACHE, "listening", ["stop", "cache"]])

    def test_a_compose_port_from_kendle_port_moves_in_the_second_stack(self):
        self.assertIn(f"starting on port {PG}", self.w.kendle("stack", "start", "db", "-f", "c1").stdout)
        self.assertEqual(self.w.kendle("stack", "wait", "-f", "c1").returncode, 0)
        self.assertIn(f"starting on port {PG + 100}", self.w.kendle("stack", "start", "db", "-f", "c2").stdout)
        self.assertEqual(self.w.kendle("stack", "wait", "-f", "c2").returncode, 0)
        self.assertTrue(answers(PG) and answers(PG + 100))

    def test_a_fixed_compose_port_is_refused_in_the_second_stack_and_says_how_to_move_it(self):
        self.w.kendle("stack", "start", "cache", "-f", "c1")
        self.assertEqual(self.w.kendle("stack", "wait", "-f", "c1").returncode, 0)
        r = self.w.kendle("stack", "start", "cache", "-f", "c2", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"cache would publish port {CACHE} in stack 2", r.stderr)
        self.assertIn(f'"${{KENDLE_PORT:-{CACHE}}}:<container port>"', r.stderr)
        self.assertIn(f"port = {CACHE}", r.stderr)
        self.assertNotIn("did not start", r.stderr)
        self.assertTrue(answers(CACHE))                        # the first stack is untouched

    def test_the_refusal_names_the_stack_it_would_run_in(self):
        review = os.path.join(self.w.ws, "review-1")
        self.w.git(self.w.app, "worktree", "add", "-q", "--detach", review, "origin/main")
        try:
            with open(os.path.join(review, "docker-compose.yml"), "w") as f:
                json.dump(self.COMPOSE, f)
            self.w.kendle("stack", "start", "cache", "-f", "c1")
            self.w.kendle("stack", "start", "db", "-f", "c2")
            r = self.w.kendle("stack", "start", "cache", cwd=review, check=False)
            self.assertEqual(r.returncode, 1)
            self.assertIn(f"cache would publish port {CACHE} in stack 3, as in the first", r.stderr)
        finally:
            self.w.kendle("stack", "stop", "-f", "review-1", check=False)
            self.w.git(self.w.app, "worktree", "remove", "--force", review)


class HeldByAnotherStack(KendleTest):
    """A service whose port does not move (shift_ports leaves it out) runs in one stack at a time."""
    TOML = f"""
        [services]
        shift_ports = []

        [services.web]
        run = "exec {sys.executable} {LISTEN} {{port}}"
        port = {free_port()}
    """

    def test_the_refusal_names_the_feature_holding_the_port(self):
        for name in ("h1", "h2"):
            self.w.kendle("new", name)
        try:
            self.w.kendle("stack", "start", "web", "-f", "h1")
            self.assertEqual(self.w.kendle("stack", "wait", "-f", "h1").returncode, 0)
            r = self.w.kendle("stack", "start", "web", "-f", "h2", check=False)
            self.assertEqual(r.returncode, 1)
            self.assertIn("is in use by the h1 stack - stop it there first: kendle stack stop -f h1", r.stderr)
        finally:
            self.w.kendle("stack", "stop", "-f", "h1", check=False)


class IntelliJ(KendleTest):
    TOML = """
        [services]
        intellij_dir = "{workspace}/runconfigs"
        shift_ports = [8080, 4200]

        [services.api]
        intellij = ["missing.xml", "Run_api.xml"]

        [services.web]
        intellij = ["npm_web.xml"]
        node_fallback = "echo no-node"
        port = 4200
        shift = ["proxy.json"]
        run_shifted = "npx serve --port {port} --proxy {shifted_dir}/proxy.json"
    """

    def test_bazel_and_npm_run_configurations(self):
        rc = os.path.join(self.w.ws, "runconfigs")
        os.makedirs(rc)
        with open(os.path.join(rc, "Run_api.xml"), "w") as f:
            f.write('<component><configuration type="BazelRunConfigurationType"><bsp-state>'
                    '<bsp-target>//api:server</bsp-target>'
                    '<handler-state programArguments="--server.port=8080 --db.url=jdbc:postgresql://localhost:5432/x '
                    '--other.url=http://localhost:8080/v1"><env key="MODE" value="dev"/></handler-state>'
                    '</bsp-state></configuration></component>')
        with open(os.path.join(rc, "npm_web.xml"), "w") as f:
            f.write('<component><configuration type="js.build_tools.npm">'
                    '<package-json value="$PROJECT_DIR$/web/package.json"/><command value="run"/>'
                    '<scripts><script value="start"/></scripts><node-interpreter value="/nonexistent/node"/>'
                    '</configuration></component>')
        self.w.kendle("new", "ij")
        os.makedirs(os.path.join(self.w.ws, "ij", "web"))
        with open(os.path.join(self.w.ws, "ij", "web", "proxy.json"), "w") as f:
            f.write('{"target": "http://localhost:8080", "db": "localhost:5432"}')
        got = self.w.py("""
            from kendle import core, services
            f = core.feature("ij")
            out = {}
            for name in ("api", "web"):
                for slot in (0, 1):
                    s = services.spec(name, f, slot)
                    out[f"{name}{slot}"] = [s["argv"], s["port"], s["env"].get("MODE")]
            print(json.dumps(out))""")
        self.assertEqual(got["api0"], [["bazel", "run", "//api:server", "--", "--server.port=8080",
                                        "--db.url=jdbc:postgresql://localhost:5432/x", "--other.url=http://localhost:8080/v1"], 8080, "dev"])
        self.assertEqual(got["api1"][0][4:], ["--server.port=8180", "--db.url=jdbc:postgresql://localhost:5432/x",
                                              "--other.url=http://localhost:8180/v1"])
        self.assertEqual(got["web0"][0][2], "echo no-node && ([ -d node_modules ] || npm ci --prefer-offline) && exec npm start")
        self.assertTrue(got["web1"][0][2].endswith(f"exec npx serve --port 4300 --proxy {self.w.state}/stack/ij/proxy.json"))
        shifted = read(os.path.join(self.w.state, "stack", "ij", "proxy.json"))
        self.assertEqual(shifted, '{"target": "http://localhost:8180", "db": "localhost:5432"}')


if __name__ == "__main__":
    unittest.main()
