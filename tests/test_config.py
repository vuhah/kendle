"""kendle.toml: the TOML reader (Python 3.9 has none), defaults, and errors a user can act on."""
import json, os, subprocess, sys, unittest
from helpers import ROOT, Workspace, KendleTest, read

sys.path.insert(0, ROOT)
from kendle import config  # noqa: E402  (config has no import-time side effects)

SAMPLE = r'''
# comment
title = "a \"q\" \\ \t \u00e9"   # trailing comment
lit = 'C:\path\{x}'
n = 1_000
f = -2.5e3
yes = true
[a.b]
arr = [
  "x", # inside
  'y',
]
"quoted key" = 3
[prompts]
ask = """
Line one {base}
  "quoted" and ""two"" \
    joined
end"""
lit_ml = QQQ
raw \n {x}QQQ
e = []
'''.replace("QQQ", "'" * 3)


class Parser(unittest.TestCase):
    def test_reads_the_subset_kendle_uses(self):
        d = config._Parser(SAMPLE).document()
        self.assertEqual(d["title"], 'a "q" \\ \t é')
        self.assertEqual(d["lit"], r"C:\path\{x}")
        self.assertEqual((d["n"], d["f"], d["yes"]), (1000, -2500.0, True))
        self.assertEqual(d["a"]["b"], {"arr": ["x", "y"], "quoted key": 3})
        self.assertEqual(d["prompts"]["ask"], 'Line one {base}\n  "quoted" and ""two"" joined\nend')
        self.assertEqual(d["prompts"]["lit_ml"], r"raw \n {x}")

    def test_same_as_tomllib_where_python_has_it(self):
        """The example profile and the sample, through both readers (needs a Python 3.11+ on PATH)."""
        newer = next((p for p in ("python3.14", "python3.13", "python3.12", "python3.11")
                      if subprocess.run(["which", p], capture_output=True).returncode == 0), None)
        if not newer:
            self.skipTest("no Python 3.11+ to compare with tomllib")
        example = read(os.path.join(ROOT, "profiles", "example", "kendle.toml"))
        for text in (SAMPLE, example):
            ref = subprocess.run([newer, "-c", "import tomllib, json, sys; print(json.dumps(tomllib.loads(sys.stdin.read())))"],
                                 input=text, capture_output=True, text=True, check=True).stdout
            self.assertEqual(config._Parser(text).document(), json.loads(ref))

    def test_rejects_broken_files_with_a_line_number(self):
        for bad, why in [('a = "x', "line 1: unterminated string"), ("a = 1\na = 2", "line 2: a is set twice"),
                         ("[[t]]", "arrays of tables"), ("a = 1 b", "unexpected 'b'"), ("a = [1 2]", "expected ','")]:
            with self.assertRaises(ValueError) as cm:
                config._Parser(bad).document()
            self.assertIn(why, str(cm.exception))

    def test_fill_leaves_unknown_fields_and_shell_braces(self):
        self.assertEqual(config.fill("{a} {b} ${HOME} {}", a=1), "1 {b} ${HOME} {}")


class Loader(KendleTest):
    def test_missing_repo_path(self):
        self.w.write_toml('[repo]\nbase = "main"\n')
        r = self.w.kendle("list", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("repo.path is required", r.stderr)

    def test_unknown_key_is_named(self):
        self.w.write_toml('[repo]\npath = "app"\nbsae = "main"\n')
        self.assertIn("unknown key repo.bsae", self.w.kendle("list", check=False).stderr)

    def test_unknown_service_key_is_named(self):
        self.w.write_toml('[repo]\npath = "app"\nbase = "main"\n[services.web]\nrun = "x"\nprot = 1\n')
        self.assertIn("unknown key services.web.prot", self.w.kendle("stack", "services", check=False).stderr)

    def test_syntax_error_names_file_and_line(self):
        self.w.write_toml('[repo]\npath = "app\n')
        err = self.w.kendle("list", check=False).stderr    # kendle's reader or tomllib (3.11+): their wording differs
        self.assertIn("kendle.toml", err)
        self.assertRegex(err, r"line 2\b")

    def test_check_reports_what_any_kendle_command_would_refuse(self):
        good = '[repo]\npath = "app"\nbase = "main"\n'
        check = lambda: config.check(self.w.ws)
        self.w.write_toml(good)
        self.assertIsNone(check())
        self.w.write_toml(good + '[services.web]\nrun = "x"\nprot = 1\n')       # caught only when services loads
        self.assertIn("unknown key services.web.prot", check())
        self.w.write_toml(good + '[review]\nslots = "three"\n')                  # a value of the wrong kind
        self.assertIn("three", check())
        self.w.write_toml('[repo]\npath = "app\n')
        self.assertRegex(check(), r"line 2\b")
        self.w.write_toml(good)

    def test_no_workspace(self):
        env = self.w.env()
        del env["KENDLE_WORKSPACE"]
        r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "kendle"), "list"], cwd=self.w.root, env=env,
                           capture_output=True, text=True)
        self.assertIn("no workspace here", r.stderr)

    def test_found_from_inside_a_subfolder(self):
        self.w.write_toml('[repo]\npath = "app"\nbase = "main"\n')
        env = self.w.env()
        del env["KENDLE_WORKSPACE"]
        out = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {ROOT!r}); "
                              "from kendle import core; print(core.HUB)"],
                             cwd=os.path.join(self.w.ws, "agent_docs"), env=env, capture_output=True, text=True).stdout
        self.assertEqual(out.strip(), self.w.ws)

    def test_defaults_and_socket_per_workspace(self):
        self.w.write_toml('[repo]\npath = "app"\nbase = "main"\n')
        got = self.w.py("""
            from kendle import core
            print(json.dumps([core.UPSTREAM, core.RESERVE_GB, core.CONFIG["disk"]["budget_gb"], core.NAME]))""",
            KENDLE_SOCKET="")
        self.assertEqual(got, ["origin/main", 10, 60, "ws"])
        other = Workspace()
        try:
            sockets = [w.py("from kendle import core; print(json.dumps(core.SOCKET))", KENDLE_SOCKET="") for w in (self.w, other)]
        finally:
            other.close()
        self.assertTrue(all(s.startswith("kendle-") for s in sockets))
        self.assertNotEqual(sockets[0], sockets[1])


if __name__ == "__main__":
    unittest.main()
