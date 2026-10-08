"""kendle autopilot: issues to merged pull requests, on a fake GitHub (tests/fakebin/gh) with fake
Claude sessions whose replies the tests write as transcripts."""
import json, os, time, unittest
from helpers import KendleTest, assistant, user, read, wait_for

TOML = """
    [review]
    host = "github"

    [autopilot]
    enabled = true
    repo = "o/r"
    authors = ["alice"]
    max_active = 1
    rounds = {rounds}
"""


def issue(n, title, author="alice", labels=("autopilot",)):
    return {"number": n, "title": title, "body": f"Body of {title}.", "state": "open",
            "user": {"login": author}, "labels": [{"name": l} for l in labels]}


class Base(KendleTest):
    ROUNDS = 3

    @classmethod
    def setUpClass(cls):
        cls.TOML = TOML.format(rounds=cls.ROUNDS)
        super().setUpClass()

    def setUp(self):
        self.gh_seed({})

    def tearDown(self):                                      # the next test starts with room for its issue
        for r in self.w.py("from kendle import autopilot; print(json.dumps(autopilot.load()))") or []:
            if r["phase"] not in ("done", "released"):
                self.w.kendle("autopilot", "release", str(r["issue"]), check=False)

    # -- the fake GitHub --
    def gh_seed(self, issues):
        with open(os.path.join(self.w.state, "gh.json"), "w") as f:
            json.dump({"issues": {str(i["number"]): i for i in issues.values()} if isinstance(issues, dict) else
                       {str(i["number"]): i for i in issues}, "labels": ["autopilot"]}, f)

    def gh(self):
        return json.loads(read(os.path.join(self.w.state, "gh.json")))

    def env(self):
        return {"FAKE_GH_ORIGIN": self.w.origin}

    def once(self):
        return self.w.kendle("autopilot", "once", **self.env()).stdout

    def rec(self, n):
        return next(r for r in self.w.py("from kendle import autopilot; print(json.dumps(autopilot.load()))")
                    if r["issue"] == n)

    def session(self, kind, feature=None):
        rows = self.w.py("from kendle import core; print(json.dumps(core.load()))")
        return [e for e in rows if e["kind"] == kind and (feature is None or e["feature"] == feature)][-1]

    # -- the fake sessions: their reply to the newest message --
    def reply(self, e, text):
        path = self.w.transcript_path(e["id"], e["cwd"])
        events = [json.loads(l) for l in read(path).splitlines()] if os.path.exists(path) else []
        time.sleep(1.05)                                     # the reply comes after the message, in a later second
        self.w.write_transcript(path, events + [user("the message"), assistant(text)])

    def commit(self, feature, name, text):
        self.w.commit(os.path.join(self.w.ws, feature), {name: text}, f"Change {name}")


class Flow(Base):
    def test_an_issue_goes_from_label_to_merged_with_a_review_round_in_between(self):
        self.gh_seed([issue(1, "Add hello"), issue(2, "Not trusted", author="mallory"),
                      issue(3, "Not labelled", labels=())])
        self.once()
        rec = self.rec(1)
        self.assertEqual((rec["phase"], rec["feature"]), ("building", "issue-1"))
        gh = self.gh()
        self.assertEqual([l["name"] for l in gh["issues"]["1"]["labels"]], ["autopilot:working"])
        self.assertIn("kendle autopilot is on it", gh["comments"][0]["body"])
        self.assertEqual(self.w.py("from kendle import autopilot; print(json.dumps([r['issue'] for r in autopilot.load()]))"), [1])
        kickoff = wait_for(lambda: [c for c in self.w.claude_calls() if c["args"] and "issue #1" in c["args"][0]])[0]
        self.assertIn("acceptEdits", kickoff["args"])
        self.assertIn("Bash(git push:*)", kickoff["args"][kickoff["args"].index("--disallowedTools"):])
        self.assertIn("Add hello", read(os.path.join(self.w.ws, "agent_docs", "issue-1", "requirement.md")))

        manager = self.session("manager", "issue-1")
        self.commit("issue-1", "hello.txt", "hello")
        self.reply(manager, "Ready: one commit, gate passed.\nAUTOPILOT: READY")
        self.once()
        rec = self.rec(1)
        self.assertEqual((rec["phase"], rec["pr"], rec["round"]), ("reviewing", 4, 1))
        pull = self.gh()["pulls"]["4"]
        self.assertTrue(pull["body"].startswith("Closes #1"))
        self.assertEqual(pull["head"]["ref"], "issue-1")
        review = self.session("review")
        self.assertEqual(review["change"], "4")

        self.reply(review, "Comments\n- hello.txt:1 - say hello to the world")
        self.once()
        self.assertEqual(self.rec(1)["phase"], "building")
        self.assertIn("round 1 of 3: Comments", self.gh()["comments"][-1]["body"])

        self.w.git(os.path.join(self.w.ws, "issue-1"), "commit", "-q", "--amend", "-m", "Say hello to the world")
        self.reply(manager, "Fixed the finding.\nAUTOPILOT: READY")
        self.once()
        rec = self.rec(1)
        self.assertEqual((rec["phase"], rec["round"]), ("reviewing", 2))
        head = self.w.git(os.path.join(self.w.ws, "issue-1"), "rev-parse", "HEAD")
        self.assertEqual(rec["sha"], head)
        self.assertEqual(self.w.git(review["cwd"], "rev-parse", "HEAD"), head)    # the review folder follows

        self.reply(review, "Approve\nLooks right now.")
        self.once()                                          # approved; its checks have not run yet
        self.assertEqual(self.rec(1)["phase"], "merging")
        gh = self.gh()
        gh["checks"][head] = [{"name": "tests", "status": "completed", "conclusion": "success"}]
        with open(os.path.join(self.w.state, "gh.json"), "w") as f:
            json.dump(gh, f)
        self.once()
        rec = self.rec(1)
        self.assertEqual(rec["phase"], "closing")
        gh = self.gh()
        self.assertEqual((gh["pulls"]["4"]["merged"], gh["pulls"]["4"]["merge_method"]), (True, "squash"))
        self.assertEqual(gh["issues"]["1"]["state"], "closed")
        self.assertEqual(gh["deleted_branches"], ["issue-1"])

        self.reply(self.session("manager", "issue-1"), "Thank you.")
        self.reply(self.session("review"), "Thanks.")
        self.once()
        self.assertEqual(self.rec(1)["phase"], "done")
        self.assertFalse(os.path.exists(os.path.join(self.w.ws, "issue-1")))
        self.assertEqual(self.gh()["issues"]["1"]["labels"], [])
        self.assertTrue(self.session("review").get("released"))


class Limits(Base):
    ROUNDS = 1

    def test_stuck_after_the_last_round_and_when_the_team_says_so(self):
        self.gh_seed([issue(1, "One round only")])
        self.once()
        manager = self.session("manager", "issue-1")
        self.commit("issue-1", "a.txt", "a")
        self.reply(manager, "AUTOPILOT: READY")
        self.once()
        self.reply(self.session("review"), "Blocked\n- a.txt:1 - wrong file")
        self.once()
        rec = self.rec(1)
        self.assertEqual(rec["phase"], "stuck")
        self.assertIn("round 1 of 1 was still Blocked", rec["why"])
        gh = self.gh()
        self.assertIn("needs-human", [l["name"] for l in gh["pulls"][str(rec["pr"])]["labels"]])
        self.assertIn("needs a person", gh["comments"][-1]["body"])
        self.assertIn("stuck", read(os.path.join(self.w.state, "autopilot.msg")))

        self.w.kendle("autopilot", "resume", "1", **self.env())
        self.assertEqual(self.rec(1)["phase"], "building")
        self.reply(manager, "I cannot reach the database.\nAUTOPILOT: STUCK the tests need a database")
        self.once()
        self.assertIn("the tests need a database", self.rec(1)["why"])

    def test_a_turn_without_a_marker_is_nudged_and_a_failing_gate_goes_back(self):
        self.gh_seed([issue(2, "Nudge me")])
        self.w.add_toml('[gate.steps.fail]\nrun = "echo the tests broke; exit 1"\n')
        try:
            self.once()
            manager = self.session("manager", "issue-2")
            self.reply(manager, "Should I use tabs or spaces?")
            self.once()
            self.assertEqual(self.rec(2)["nudges"], 1)
            screen = lambda: self.w.tmux("capture-pane", "-p", "-J", "-t", manager["pane"])
            self.assertTrue(wait_for(lambda: "Nobody is here to answer" in screen()), screen())   # typed into its pane
            self.commit("issue-2", "b.txt", "b")
            self.reply(manager, "Spaces.\nAUTOPILOT: READY")
            self.once()
            rec = self.rec(2)
            self.assertEqual((rec["phase"], rec["fixes"], rec["pr"]), ("building", 1, None))
            self.assertTrue(any("kendle gate failed" in h for h in rec["history"]))
        finally:
            self.w.write_toml(read(os.path.join(self.w.ws, "kendle.toml")).split("[gate.steps.fail]")[0])

    def test_adopt_hands_a_waiting_team_to_the_loop_with_its_conversation(self):
        self.gh_seed([issue(5, "Already started")])
        self.w.kendle("new", "early")
        sid = self.w.kendle("manager", "early").stdout.split()[1]
        m = self.session("manager", "early")
        self.reply(m, "Do you approve the spec?")
        self.w.kendle("autopilot", "adopt", "5", "early", **self.env())
        call = wait_for(lambda: [c for c in self.w.claude_calls() if c["args"] and "issue #5" in c["args"][0]])[0]
        self.assertEqual(call["args"][call["args"].index("--resume") + 1], sid)        # its own conversation
        self.assertIn("acceptEdits", call["args"])
        self.assertNotIn("requirement.md", call["args"][0])                           # it has its own docs
        self.assertEqual((self.rec(5)["phase"], self.rec(5)["feature"]), ("building", "early"))
        self.assertEqual([l["name"] for l in self.gh()["issues"]["5"]["labels"]], ["autopilot:working"])
        r = self.w.kendle("autopilot", "adopt", "5", "early", check=False, **self.env())
        self.assertIn("already has issue #5", r.stderr)
        self.gh_seed([issue(6, "By someone else", author="mallory")])
        self.w.kendle("new", "other")
        r = self.w.kendle("autopilot", "adopt", "6", "other", check=False, **self.env())
        self.assertIn("not one of [autopilot] authors", r.stderr)

    def test_refuses_to_run_unless_enabled_with_authors(self):
        self.w.add_toml("")
        r = self.w.kendle("autopilot", "once", check=False, KENDLE_WORKSPACE=self.w.ws)
        self.assertEqual(r.returncode, 0)
        text = read(os.path.join(self.w.ws, "kendle.toml"))
        self.w.write_toml(text.replace("enabled = true", "enabled = false"))
        try:
            r = self.w.kendle("autopilot", "once", check=False)
            self.assertEqual(r.returncode, 1)
            self.assertIn("enabled = true", r.stderr)
        finally:
            self.w.write_toml(text)


if __name__ == "__main__":
    unittest.main()
