"""Features, sessions and their states - with a fake claude, on a tmux socket of the test's own."""
import json, os, subprocess, time, unittest
from helpers import RUN, KendleTest, assistant, user, read, wait_for, sh


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
        (a TaskStop in the manager's transcript) does not; neither does one silent for 30 minutes.
        One that replied and waits is idle."""
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
        self.assertEqual(states, {"builder": "working", "tester": "stopped", "reviewer": "stopped", "planner": "idle"})

    def test_a_waiting_role_is_idle_however_long_ago_it_replied(self):
        """A role that replied and waits is idle - an hour after its reply, and after a TaskStop too.
        `kendle list` says idle for it and never done."""
        sid = self.manager("longwait")
        path = self.w.transcript_path(sid, os.path.join(self.w.ws, "longwait"))
        subs = os.path.join(path[:-len(".jsonl")], "subagents")
        os.makedirs(subs)
        old = time.time() - 3600
        for name in ("planner", "auditor"):
            p = os.path.join(subs, f"agent-{name}.jsonl")
            self.w.write_transcript(p, [user("brief", ago=3600), assistant("spec written", ago=3600)])
            os.utime(p, (old, old))
            with open(os.path.join(subs, f"agent-{name}.meta.json"), "w") as f:
                json.dump({"name": name}, f)
        self.w.write_transcript(path, [user("go"), assistant(stop="tool_use", tool=("TaskStop", {"task_id": "auditor"}))])
        states = {s["name"]: s["state"] for s in self.state("longwait")["internal"]}
        self.assertEqual(states, {"planner": "idle", "auditor": "idle"})
        out = self.w.kendle("list").stdout
        lines = out.splitlines()                                # the longwait block: its header to the next one
        start = next(i for i, l in enumerate(lines) if l.startswith("\033[1mlongwait\033[0m"))
        end = next((i for i in range(start + 1, len(lines)) if not lines[i].startswith(" ")), len(lines))
        rows = {l.split()[1]: l.split()[2] for l in lines[start + 1:end] if "internal" in l}
        self.assertEqual(rows, {"planner": "idle", "auditor": "idle"}, out)   # idle, never done

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

    def test_a_session_held_at_the_folder_trust_question_says_so(self):
        marker = os.path.join(self.w.state, "fake-untrusted")
        open(marker, "w").close()
        try:
            self.manager("trusty")
            self.w.kendle("ask", "is the folder trusted")
            self.assertTrue(wait_for(lambda: self.state("trusty")["state"] == "trust?"), self.state("trusty"))
            asked = lambda: [q for q in json.loads(self.w.kendle("list", "--json").stdout)["ask"]
                             if q["question"] == "is the folder trusted"]
            self.assertTrue(wait_for(lambda: asked() and asked()[0]["state"] == "trust?"), asked())
        finally:
            os.remove(marker)
            self.w.kendle("stop", "trusty")
        self.manager("trusted")
        call = wait_for(lambda: self.w.claude_calls())
        self.assertTrue(call)
        time.sleep(0.5)
        self.assertEqual(self.state("trusted")["state"], "idle")            # no question asked: still idle
        self.w.kendle("stop", "trusted")

    def test_two_starts_at_once_make_one_manager(self):
        self.w.kendle("new", "twice")
        runs = [subprocess.Popen(RUN + ["manager", "twice"], cwd=self.w.ws, env=self.w.env(), stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True) for _ in range(2)]
        outs = [p.communicate(timeout=60)[0].split() for p in runs]
        self.assertEqual(sorted(o[0] for o in outs), ["already", "started"])                 # one found the other's
        self.assertEqual(outs[0][-2], outs[1][-2])                                         # the same session
        managers = [e for e in self.w.py("from kendle import core; print(json.dumps(core.load()))")
                    if e["kind"] == "manager" and e["feature"] == "twice"]
        self.assertEqual(len(managers), 1)
        self.w.kendle("stop", "twice")

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
        self.assertEqual(args[args.index("--add-dir") + 1], os.path.join(self.w.ws, "agent_docs"))
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


class SidebarRows(KendleTest):
    """How the sidebar draws a session row: the state word, its colour, and whether the name is dimmed."""
    ASK = False

    def draw(self, kind, state):
        return self.w.py(f"""
            from kendle.cmd import sidebar
            s = sidebar.Sidebar.__new__(sidebar.Sidebar)
            s.slot, s.viewer, calls = None, None, []
            s.put = lambda y, x, text, color="text", selected=False, attr=0: calls.append([text, color])
            e = {{"id": "x", "name": "planner", "state": {state!r}, "ctx": None, "pane": None}}
            s.row(0, {{"kind": {kind!r}, "e": e}}, False, 60)
            print(json.dumps(calls))
        """)

    def test_an_idle_role_is_dimmed_and_an_idle_manager_is_not(self):
        self.assertIn(["idle", "faint"], self.draw("internal", "idle"))
        self.assertIn(["planner", "dim"], self.draw("internal", "idle"))
        self.assertIn(["idle", "faint"], self.draw("manager", "idle"))
        self.assertIn(["manager", "text"], self.draw("manager", "idle"))
        self.assertIn(["planner", "text"], self.draw("sub", "idle"))
        self.assertIn(["manager", "dim"], self.draw("manager", "stopped"))
        self.assertIn(["working", "green"], self.draw("internal", "working"))


class FitScreen(KendleTest):
    """core.fit_screen repaints the whole pane only when it must: at start-up, when the real size
    changed since it last fitted the pane, or when the size cannot be read - and it never raises.

    Curses is faked as it behaves with LINES and COLUMNS exported: on every resize ncurses hands the
    pane back its pinned 50x34 first, and only resize_term gives it the real size."""

    def clears(self, steps):
        """Run fit_screen once per (start, real size) step; a size of None fails the size read, "error"
        makes resize_term fail. Returns how often each step cleared the screen."""
        return self.w.py(f"""
            import curses, os
            from kendle import core
            class Scr:
                clears, size = 0, (50, 34)
                def getmaxyx(self):
                    return self.size
                def clear(self):
                    self.clears += 1
            real = [None]
            def size(fd):
                if real[0] is None:
                    raise OSError("not a terminal")
                return os.terminal_size((34, real[0]) if real[0] != "error" else (34, 1))
            def resize_term(lines, cols):
                if real[0] == "error":
                    raise curses.error("resize_term failed")
                scr.size = (lines, cols)
            os.get_terminal_size, curses.resize_term, curses.update_lines_cols = size, resize_term, lambda: None
            scr, out = Scr(), []
            for start, lines in {steps!r}:
                real[0], before, scr.size = lines, scr.clears, (50, 34)
                core.fit_screen(scr, start)
                out.append(scr.clears - before)
            print(json.dumps(out))
        """)

    def test_start_up_clears_and_an_unchanged_size_does_not(self):
        self.assertEqual(self.clears([(True, 40), (False, 40), (False, 40), (True, 40)]), [1, 0, 0, 1])

    def test_a_changed_size_clears_once(self):
        self.assertEqual(self.clears([(True, 40), (False, 60), (False, 60), (False, 50), (False, 50)]), [1, 1, 0, 1, 0])

    def test_a_failed_size_read_clears_and_does_not_raise(self):
        self.assertEqual(self.clears([(True, 40), (False, None), (False, "error"), (False, 40)]), [1, 1, 1, 1])


def bottom_rows(frame):
    """How many rows at the foot of a sidebar frame keep to the bottom: from the detail separator
    down (the detail lines, then the key hint or a message wrapped over up to MSG_LINES rows)."""
    seps = [i for i, line in enumerate(frame) if line.strip().startswith("─")]
    return len(frame) - seps[-1] if seps else 1


class ConsoleResize(KendleTest):
    """A resized window repaints the sidebar and the logs column whole, at the new size.

    The console starts with LINES and COLUMNS exported (as some shells do), pinned to the sidebar's
    first size: curses then believes them over the terminal, so only reading the real size on a
    resize keeps the frame right - without it old rows stay and the header scrolls away."""

    SIDEBAR = "kendle:console.0"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.w.py("from kendle import core; core.ensure_session()", KENDLE_NO_SIDEBAR="0", LINES="50", COLUMNS="34")

    def setUp(self):
        self.w.py("from kendle import core; core.set_sidebar_width(34)")
        self.w.tmux("resize-window", "-t", "kendle:console", "-x", "200", "-y", "50")
        self.w.tmux("resize-pane", "-t", self.SIDEBAR, "-x", "34")
        self.w.tmux("respawn-pane", "-k", "-t", self.SIDEBAR)
        self.settle(self.SIDEBAR)

    def capture(self, pane):
        out = sh("tmux", "-L", self.w.socket, "capture-pane", "-p", "-t", pane, check=False).stdout
        return out[:-1].split("\n") if out.endswith("\n") else out.split("\n")

    def size(self, pane):
        w, h = self.w.tmux("display", "-p", "-t", pane, "#{pane_width} #{pane_height}").split()
        return int(w), int(h)

    def settle(self, pane):
        """The pane's frame once two captures in a row agree."""
        time.sleep(0.5)
        last = [None]

        def same():
            now = self.capture(pane)
            done, last[0] = now == last[0] and any(line.strip() for line in now), now
            return done
        self.assertTrue(wait_for(same, timeout=15, step=0.4), "the pane never settled:\n" + "\n".join(last[0] or []))
        return last[0]

    def assert_clean(self, old, new, pane, header, bottom):
        """No row of the old frame left where the new frame does not draw it: rows from the top keep
        their row, the bottom `bottom` rows move with the new height; the header is on row 0 only."""
        width, height = self.size(pane)
        shown = "\n".join(new)
        self.assertEqual(len(new), height, shown)
        self.assertTrue(all(len(line) <= width for line in new), shown)
        heads = [i for i, line in enumerate(new) if line.split()[:1] == [header]]
        self.assertEqual(heads, [0], shown)
        for row, line in enumerate(new):
            was = [i for i, o in enumerate(old) if line.strip() and o == line]
            if was:
                want = [i if i < len(old) - bottom else i - len(old) + height for i in was]
                self.assertIn(row, want, f"row {row} {line!r} is left from the old frame:\n{shown}")

    def resize_and_check(self, x, y, pane=None, header="ws", bottom=None):
        pane = pane or self.SIDEBAR
        old = self.capture(pane)
        self.w.tmux("resize-window", "-t", "kendle:console", "-x", str(x), "-y", str(y))
        self.assert_clean(old, self.settle(pane), pane, header, bottom or bottom_rows(old))

    def test_taller_and_shorter_windows_repaint_the_sidebar(self):
        self.resize_and_check(200, 70)
        self.resize_and_check(200, 30)

    def test_a_height_only_resize_repaints_the_sidebar(self):
        self.resize_and_check(200, 40)
        self.assertEqual(self.size(self.SIDEBAR), (34, 40))

    def test_wider_and_narrower_windows_keep_the_chosen_width(self):
        self.resize_and_check(250, 60)
        self.resize_and_check(150, 40)
        self.assertEqual(self.size(self.SIDEBAR)[0], 34)

    def test_the_logs_column_repaints_too(self):
        logs = self.w.py("""
            from kendle import core
            core.ui_set("logs_mode", "off")
            core.toggle_logs(True)
            print(json.dumps(core.tmux("show", "-gqv", "@kendle_logs")))""")
        self.addCleanup(self.w.py, "from kendle import core; core.toggle_logs(False); core.ui_set('logs_mode', 'auto')")
        self.settle(logs)
        self.resize_and_check(200, 30, pane=logs, header="logs", bottom=1)
        self.resize_and_check(200, 70, pane=logs, header="logs", bottom=1)

    def test_a_resize_right_after_a_respawn_settles_clean(self):
        old = self.capture(self.SIDEBAR)
        self.w.tmux("respawn-pane", "-k", "-t", self.SIDEBAR)
        self.w.tmux("resize-window", "-t", "kendle:console", "-x", "200", "-y", "35")
        self.assert_clean(old, self.settle(self.SIDEBAR), self.SIDEBAR, "ws", bottom_rows(old))

    def test_a_dragged_width_survives_a_window_resize(self):
        width = lambda: self.w.py("from kendle import core; print(core.sidebar_width())")
        self.w.tmux("resize-pane", "-t", self.SIDEBAR, "-x", "40")
        self.assertEqual(wait_for(lambda: width() == 40 and 40), 40)
        self.w.tmux("resize-window", "-t", "kendle:console", "-x", "220", "-y", "45")
        self.assertTrue(wait_for(lambda: self.size(self.SIDEBAR)[0] == 40), self.size(self.SIDEBAR))
        self.assertEqual(width(), 40)


if __name__ == "__main__":
    unittest.main()
