"""Features, sessions and their states - with a fake claude, on a tmux socket of the test's own."""
import json, os, time, unittest
from helpers import KendleTest, assistant, user, read, wait_for, sh


class Features(KendleTest):
    def test_new_feature_branches_from_the_fetched_base(self):
        self.w.kendle("new", "fix-login")
        path = os.path.join(self.w.ws, "fix-login")
        self.assertEqual(self.w.git(path, "rev-parse", "--abbrev-ref", "HEAD"), "fix-login")
        self.assertEqual(self.w.git(path, "rev-parse", "HEAD"), self.w.git(self.w.app, "rev-parse", "origin/main"))
        meta = json.loads(read(os.path.join(self.w.ws, "agent_docs", "fix-login", "session.json")))
        self.assertEqual((meta["branch"], meta["base"], meta["tool"]), ("fix-login", "origin/main", "kendle"))
        self.assertEqual(self.w.git(path, "status", "--porcelain"), "")       # links are excluded
        listed = json.loads(self.w.kendle("list", "--json").stdout)
        self.assertIn("fix-login", [f["name"] for f in listed["features"]])
        self.assertNotIn("ask", [f["name"] for f in listed["features"]])

    def test_names_that_are_not_features(self):
        for name in ("ask", "agent_docs", "app", "review-1", "Bad_Name", "-x"):
            r = self.w.kendle("new", name, check=False)
            self.assertEqual(r.returncode, 1, name)
        self.w.kendle("new", "twice")
        self.assertIn("already exists", self.w.kendle("new", "twice", check=False).stderr)


class Sessions(KendleTest):
    TOML = """
        [task]
        fetch = "echo {id}"
    """

    def manager(self, name):
        self.w.kendle("new", name)
        out = self.w.kendle("manager", name).stdout.split()
        return out[1]                                           # "started <id> <pane>"

    def state(self, feature):
        tree = json.loads(self.w.kendle("list", "--json").stdout)["features"]
        f = next(f for f in tree if f["name"] == feature)
        return f["managers"][0]

    def test_ask_runs_read_only_on_the_desk_with_the_base_in_its_prompt(self):
        self.w.kendle("ask", "why is login slow")
        call = wait_for(lambda: [c for c in self.w.claude_calls() if "why is login slow" in c["args"]])[0]
        self.assertEqual(call["cwd"], os.path.join(self.w.ws, "ask"))
        args = call["args"]
        self.assertIn("plan", args[args.index("--permission-mode") + 1])
        self.assertIn("Edit", args)
        note = args[args.index("--append-system-prompt") + 1]
        self.assertIn("latest origin/main", note)
        self.assertIn("kendle task", note)                       # a tracker is set up, so it is told

    def test_manager_states_follow_the_transcript(self):
        sid = self.manager("states")
        call = wait_for(lambda: [c for c in self.w.claude_calls() if sid in c["args"]])[0]
        self.assertIn("feature team in the kendle console", call["args"][call["args"].index("--append-system-prompt") + 1])
        self.assertEqual(self.state("states")["state"], "idle")             # no transcript yet
        path = self.w.transcript_path(sid, os.path.join(self.w.ws, "states"))
        self.w.write_transcript(path, [user("build it"), assistant(stop="tool_use", tool=("Bash", {"command": "make"}))])
        self.assertEqual(self.state("states")["state"], "working")
        self.w.write_transcript(path, [user("build it"), assistant("done", stop="end_turn")])
        self.assertEqual(self.state("states")["state"], "waiting")
        self.assertIn("already running", self.w.kendle("manager", "states").stdout)
        self.w.kendle("stop", "states")
        self.assertEqual(self.state("states")["state"], "stopped")

    def test_a_resumed_session_cut_off_mid_turn_is_not_working(self):
        """Events from before the session's process started are history: it waits for you."""
        sid = self.manager("resumed")
        path = self.w.transcript_path(sid, os.path.join(self.w.ws, "resumed"))
        self.w.write_transcript(path, [user("go", ago=7200), assistant(stop="tool_use", tool=("Bash", {}), ago=7200)])
        self.assertEqual(self.state("resumed")["state"], "idle")

    def test_quiet_is_not_dead_but_stopped_is(self):
        """A role blocked in one long tool call still counts as working; one its manager stopped
        (a TaskStop in the manager's transcript) does not; neither does one silent for 30 minutes."""
        sid = self.manager("team")
        path = self.w.transcript_path(sid, os.path.join(self.w.ws, "team"))
        subs = os.path.join(path[:-len(".jsonl")], "subagents")
        os.makedirs(subs)
        for name in ("builder", "tester", "reviewer", "planner"):
            self.w.write_transcript(os.path.join(subs, f"agent-{name}.jsonl"),
                                    [user("brief", ago=600), assistant(stop="tool_use", tool=("Bash", {}), ago=600)])
            with open(os.path.join(subs, f"agent-{name}.meta.json"), "w") as f:
                json.dump({"name": name}, f)
        self.w.write_transcript(os.path.join(subs, "agent-planner.jsonl"), [user("brief"), assistant("spec written")])
        old = time.time() - 3600
        os.utime(os.path.join(subs, "agent-reviewer.jsonl"), (old, old))
        self.w.write_transcript(path, [user("go"), assistant(stop="tool_use", tool=("TaskStop", {"task_id": "tester"}))])
        states = {s["name"]: s["state"] for s in self.state("team")["internal"]}
        self.assertEqual(states, {"builder": "working", "tester": "stopped", "reviewer": "stopped", "planner": "done"})

    def test_sub_agents_are_read_only(self):
        sid = self.manager("subs")
        out = self.w.kendle("sub", "subs", "probe", "look at the login code").stdout.split()
        call = wait_for(lambda: [c for c in self.w.claude_calls() if out[1] in c["args"]])[0]
        args = call["args"]
        self.assertEqual(args[args.index("--permission-mode") + 1], "plan")
        for tool in ("Edit", "Write", "NotebookEdit", "ExitPlanMode"):
            self.assertIn(tool, args)
        self.assertIn("'subs' manager session", args[args.index("--append-system-prompt") + 1])
        self.assertTrue(any(s["name"] == "probe" for s in self.state("subs")["subs"]))

    def test_fresh_stops_the_manager_and_starts_one_from_the_docs(self):
        first = self.manager("restart")
        out = self.w.kendle("fresh", "restart").stdout.split()
        self.assertNotEqual(out[2], first)
        call = wait_for(lambda: [c for c in self.w.claude_calls() if out[2] in c["args"]])[0]
        self.assertIn("Read agent_docs/restart/team.md first", call["args"][0])

    def test_promote_turns_a_question_into_a_feature(self):
        qid = self.w.kendle("ask", "can checkout retry twice?").stdout.split()[1]
        path = self.w.transcript_path(qid, os.path.join(self.w.ws, "ask"))
        self.w.write_transcript(path, [user("can checkout retry twice?"),
                                       assistant("Bug - a retry charges twice.\nSuggested feature name: retry-once")])
        out = self.w.kendle("promote", qid[:8], "retry-once").stdout
        self.assertIn("promoted to retry-once", out)
        req = read(os.path.join(self.w.ws, "agent_docs", "retry-once", "requirement.md"))
        self.assertIn("can checkout retry twice?", req)
        self.assertIn("Bug - a retry charges twice.", req)
        self.assertIn("answered on main @", req)
        mid = out.split()[-1]
        call = wait_for(lambda: [c for c in self.w.claude_calls() if mid in c["args"]])[0]
        self.assertIn("agent_docs/retry-once/requirement.md", call["args"][0])


class TeamFlags(KendleTest):
    def calls_for(self, feature):
        self.w.kendle("new", feature)
        sid = self.w.kendle("manager", feature).stdout.split()[1]
        return wait_for(lambda: [c for c in self.w.claude_calls() if sid in c["args"]])[0]["args"]

    def test_unset_keeps_everything_set_trims(self):
        args = self.calls_for("plain")
        self.assertNotIn("--strict-mcp-config", args)
        self.assertNotIn("--settings", args)
        self.assertFalse(any("kendle task" in a for a in args))  # no tracker: no hint
        self.w.add_toml('[team]\nmcp_servers = []\ndisabled_plugins = ["some-plugin@market"]\n')
        args = self.calls_for("lean")
        self.assertIn("--strict-mcp-config", args)
        cfg = json.loads(read(args[args.index("--mcp-config") + 1]))
        self.assertEqual(cfg, {"mcpServers": {}})
        self.assertEqual(json.loads(args[args.index("--settings") + 1]), {"enabledPlugins": {"some-plugin@market": False}})


class Console(KendleTest):
    def test_panes_socket_and_key_bindings(self):
        self.w.py("from kendle import core; core.ensure_session()", KENDLE_NO_SIDEBAR="0")
        panes = self.w.tmux("list-panes", "-t", "kendle:console", "-F", "#{pane_index} #{pane_start_command}").splitlines()
        self.assertEqual(len(panes), 2)
        self.assertTrue(panes[0].endswith("sidebar"), panes)
        self.assertTrue(panes[1].endswith("view idle"), panes)
        self.assertEqual(self.w.tmux("show-environment", "-g", "KENDLE_WORKSPACE"), f"KENDLE_WORKSPACE={self.w.ws}")
        drawn = wait_for(lambda: "ws" in self.w.tmux("capture-pane", "-p", "-t", "kendle:console.0").split("\n")[0])
        self.assertTrue(drawn, self.w.tmux("capture-pane", "-p", "-t", "kendle:console.0"))
        keys = self.w.tmux("list-keys")
        self.assertIn("select-pane -t kendle:console.0", keys)
        self.assertIn("respawn-pane -k -t kendle:console.0", keys)

    def test_show_swaps_a_session_in_and_stop_puts_the_placeholder_back(self):
        self.w.py("from kendle import core; core.ensure_session()")
        self.w.kendle("new", "shown")
        sid = self.w.kendle("manager", "shown").stdout.split()[1]
        self.w.kendle("show", "shown")
        slot = lambda: self.w.tmux("list-panes", "-t", "kendle:console", "-F", "#{pane_index} #{pane_start_command}").splitlines()[1]
        self.assertIn(sid, slot())
        self.w.kendle("stop", "shown")
        self.assertTrue(wait_for(lambda: slot().endswith("view idle")), slot())

    def test_transcripts_are_reread_only_when_they_change(self):
        """Keys must never wait on data: the same file at the same size and time is not parsed again."""
        path = self.w.transcript_path("cache-test", self.w.ws)
        self.w.write_transcript(path, [user("hi"), assistant("hello")])
        got = self.w.py(f"""
            from kendle import core
            calls = []
            real = core.events_backwards
            core.events_backwards = lambda p, *a: calls.append(p) or real(p, *a)
            first = [core.turn_state({path!r}) for _ in range(5)]
            with open({path!r}, "a") as f:
                f.write(json.dumps({{"type": "user", "message": {{"content": "more"}}}}) + "\\n")
            second = core.turn_state({path!r})
            print(json.dumps([first, second, len(calls)]))""")
        self.assertEqual(got, [["waiting"] * 5, "working", 2])


if __name__ == "__main__":
    unittest.main()
