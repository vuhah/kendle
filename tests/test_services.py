"""kendle stack / kendle services: real processes on free ports, two stacks side by side."""
import json, os, shutil, socket, subprocess, sys, time, unittest
from helpers import FAKEBIN, KendleTest, read, wait_for


def free_port():
    """A port with its +100 neighbour free too (the second stack's), away from common dev ports."""
    import random
    for _ in range(200):
        port = random.randrange(20000, 40000)
        try:
            for p in (port, port + 100):
                with socket.socket() as s:
                    s.bind(("127.0.0.1", p))
            return port
        except OSError:
            continue
    raise RuntimeError("no free port pair")


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


class Compose(KendleTest):
    TOML = """
        [services.cache]
        compose = "cache"
    """

    def test_compose_service_comes_from_the_compose_file(self):
        if not shutil.which("docker"):
            self.skipTest("docker is not installed")
        self.w.kendle("new", "c1")
        with open(os.path.join(self.w.ws, "c1", "docker-compose.yml"), "w") as f:
            f.write('services:\n  cache:\n    image: redis:7\n    ports:\n      - "16379:6379"\n')
        got = self.w.py("""
            from kendle import core, services
            s = services.spec("cache", core.feature("c1"), 0)
            print(json.dumps([s["argv"][-4:], s["port"], s["up_when"], s["stop"][-2:]]))""")
        self.assertEqual(got, [["kendle-c1", "up", "--no-log-prefix", "cache"], 16379, "listening", ["stop", "cache"]])


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
