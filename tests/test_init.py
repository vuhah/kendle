"""kendle init: a workspace from a clone or an existing checkout, never overwriting anything."""
import os, subprocess, unittest
from helpers import RUN, Workspace, sh, read


class Init(unittest.TestCase):
    def setUp(self):
        self.w = Workspace(ask=False)              # only for its origin; init makes its own workspace
        self.ws = os.path.join(self.w.root, "fresh")
        os.makedirs(self.ws)

    def tearDown(self):
        self.w.close()

    def init(self, *args, cwd=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("KENDLE_")}
        return subprocess.run([*RUN, "init", *args], cwd=cwd or self.ws, capture_output=True, text=True, env=env)

    def test_clone_writes_everything_and_finds_the_base(self):
        r = self.init("--clone", self.w.origin)
        self.assertEqual(r.returncode, 0, r.stderr)
        for path in ("kendle.toml", "CLAUDE.md", "agent_docs", "ask", "origin"):
            self.assertTrue(os.path.exists(os.path.join(self.ws, path)), path)
        roles = sorted(os.listdir(os.path.join(self.ws, "roles")))
        self.assertEqual(roles, [f"kendle-{r}.md" for r in ("auditor", "builder", "planner", "reviewer", "shipper", "tester")])
        toml = read(os.path.join(self.ws, "kendle.toml"))
        self.assertIn('path = "origin"', toml)
        self.assertIn('base = "main"', toml)
        claude = read(os.path.join(self.ws, "CLAUDE.md"))
        self.assertIn("origin/main", claude)
        self.assertNotRegex(claude, r"\{(name|repo|upstream)\}")
        self.assertEqual(sh("git", "-C", os.path.join(self.ws, "ask"), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "HEAD")

    def test_existing_checkout_and_refusals(self):
        sh("git", "clone", "-q", self.w.origin, os.path.join(self.ws, "shop"))
        self.assertEqual(self.init().returncode, 0)
        r = self.init()
        self.assertIn("already a workspace", r.stderr)
        os.remove(os.path.join(self.ws, "kendle.toml"))
        with open(os.path.join(self.ws, "CLAUDE.md"), "w") as f:
            f.write("mine\n")
        r = self.init("shop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("kept     CLAUDE.md", r.stdout)
        self.assertEqual(read(os.path.join(self.ws, "CLAUDE.md")), "mine\n")

    def test_errors_a_user_can_act_on(self):
        self.assertIn("no git checkout in this folder", self.init().stderr)
        sh("git", "init", "-q", os.path.join(self.ws, "a"))
        self.assertIn("has no remote", self.init("a").stderr)
        sh("git", "init", "-q", os.path.join(self.ws, "b"))
        self.assertIn("several checkouts here (a, b)", self.init().stderr)
        sh("git", "clone", "-q", self.w.origin, os.path.join(self.ws, "c"))
        self.assertIn("origin/nope does not exist", self.init("c", "--base", "nope").stderr)

    def test_a_feature_in_the_new_workspace_sees_roles_and_docs_and_stays_clean(self):
        self.assertEqual(self.init("--clone", self.w.origin).returncode, 0)
        env = {k: v for k, v in os.environ.items() if not k.startswith("KENDLE_")}
        env.update(KENDLE_STATE=os.path.join(self.w.root, "state2"), KENDLE_SOCKET=self.w.socket)
        r = subprocess.run([*RUN, "new", "one"], cwd=self.ws, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        feature = os.path.join(self.ws, "one")
        self.assertEqual(os.path.realpath(os.path.join(feature, "agent_docs")), os.path.join(self.ws, "agent_docs"))
        self.assertIn("kendle-builder.md", os.listdir(os.path.join(feature, ".claude", "agents")))
        self.assertEqual(sh("git", "-C", feature, "status", "--porcelain").stdout, "")


if __name__ == "__main__":
    unittest.main()
