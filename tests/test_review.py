"""Reviews of someone else's change, from Gerrit, GitHub and GitLab refs on a scratch origin."""
import os, subprocess, sys, time, unittest
from helpers import FAKEBIN, RUN, KendleTest, assistant, free_port, read, user, wait_for


class Reviews(KendleTest):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        w = cls.w
        first = w.push_ref({"a.txt": "1"}, "Login - first try", "refs/changes/34/1234/1")
        w.push_ref({"a.txt": "2"}, "Login - second try", "refs/changes/34/1234/2", parent=first)
        one = w.push_ref({"p1.txt": "1"}, "PR commit one", "refs/pull/7/head")
        w.push_ref({"p2.txt": "2"}, "PR commit two", "refs/pull/7/head", parent=one)
        w.push_ref({"m.txt": "1"}, "MR only commit", "refs/merge-requests/9/head")

    def tearDown(self):
        for row in self.w.kendle("reviews").stdout.splitlines():
            self.w.kendle("review-close", row.split()[1].split("/")[0], check=False)

    def host(self, name):
        text = read(os.path.join(self.w.ws, "kendle.toml")).split("\n[review]")[0]
        self.w.write_toml(text + f'\n[review]\nhost = "{name}"\n')

    def brief_for(self, number):
        calls = wait_for(lambda: [c for c in self.w.claude_calls() if c["args"] and c["args"][0].startswith("Review ")
                                  and f" {number}" in c["args"][0]])
        return calls[-1]

    def test_gerrit_takes_the_newest_patch_set_even_from_an_older_url(self):
        self.host("gerrit")
        out = self.w.kendle("review", "https://review.example.com/c/app/+/1234/1").stdout
        self.assertIn("review 1234/2 'Login - second try' by Tester", out)
        call = self.brief_for(1234)
        self.assertIn("Review Gerrit change 1234, patch set 2", call["args"][0])
        self.assertIn("1 files. The diff is HEAD against HEAD^", call["args"][0])
        self.assertEqual(read(os.path.join(call["cwd"], "a.txt")), "2")

    def test_github_and_gitlab_review_the_whole_change_against_its_merge_base(self):
        self.host("github")
        self.w.kendle("review", "https://github.com/x/app/pull/7")
        self.assertIn("2 files. The diff is HEAD against", self.brief_for(7)["args"][0])
        self.host("gitlab")
        self.w.kendle("review", "https://gitlab.com/x/app/-/merge_requests/9")
        self.assertIn("Review merge request 9 at", self.brief_for(9)["args"][0])

    def test_the_host_is_guessed_from_the_remote_or_asked_for(self):
        self.w.write_toml(read(os.path.join(self.w.ws, "kendle.toml")).split("\n[review]")[0])
        r = self.w.kendle("review", "7", check=False)
        self.assertIn("which review host?", r.stderr)

    def test_titles_follow_the_host_each_review_was_opened_from(self):
        self.host("gerrit")
        self.w.kendle("review", "1234")
        self.host("github")
        self.w.kendle("review", "7")
        got = self.w.py("""
            from kendle import core, review
            print(json.dumps(sorted(review.title(e).split(" - ")[0] for e in core.reviews())))""")
        self.assertEqual(got, ["Gerrit change 1234 patch set 2", "Pull request 7 at " + got[1].split()[-1]])

    def test_close_by_change_number_saves_findings_and_frees_the_folder(self):
        self.host("gerrit")
        out = self.w.kendle("review", "1234").stdout
        folder = out.rsplit(" in ", 1)[1].strip()
        sid = next(e for e in self.w.py("from kendle import core; print(json.dumps(core.reviews()))") if e["change"] == "1234")["id"]
        self.w.write_transcript(self.w.transcript_path(sid, folder),
                                [{"type": "user", "message": {"content": "review it"}},
                                 {"type": "assistant", "message": {"stop_reason": "end_turn", "content": [
                                     {"type": "text", "text": "Comments\n- a.txt:1 - the value is hard-coded."}]}}])
        out = self.w.kendle("review-close", "1234").stdout
        self.assertIn("closed review 1234", out)
        saved = read(os.path.join(self.w.ws, "agent_docs", "reviews", "1234-2.md"))
        self.assertIn("# Gerrit change 1234 patch set 2 - Login - second try", saved)
        self.assertIn("a.txt:1 - the value is hard-coded.", saved)
        self.assertFalse(os.path.exists(folder))

    def test_starting_reviews_at_once_never_doubles_one_or_shares_a_folder(self):
        self.host("gerrit")
        self.w.push_ref({"b.txt": "1"}, "Logout", "refs/changes/78/5678/1")
        runs = [subprocess.Popen(RUN + ["review", c], cwd=self.w.ws, env=self.w.env(), stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True) for c in ("1234", "1234", "5678")]
        outs = [p.communicate(timeout=120) for p in runs]
        self.assertEqual([p.returncode for p in runs], [0, 0, 0], outs)
        open_ = self.w.py("from kendle import core; print(json.dumps(core.reviews()))")
        self.assertEqual(sorted(e["change"] for e in open_), ["1234", "5678"])         # one review of 1234, not two
        self.assertEqual(len({e["cwd"] for e in open_}), 2)                          # each in a folder of its own

    def test_at_most_three_at_once(self):
        self.host("gerrit")
        self.w.kendle("review", "1234")
        self.host("github")
        self.w.kendle("review", "7")
        self.host("gitlab")
        self.w.kendle("review", "9")
        self.assertEqual(len(self.w.kendle("reviews").stdout.splitlines()), 3)
        self.host("gerrit")
        self.w.kendle("review", "1234")                           # already open: comes back, no fourth slot
        self.assertEqual(len(self.w.kendle("reviews").stdout.splitlines()), 3)


class ReviewStacks(KendleTest):
    """A review may run its folder's services; they stop when the review closes or the folder's code moves."""
    TOML = f"""
        [services.web]
        run = "exec {sys.executable} {os.path.join(FAKEBIN, 'listen')} {{port}}"
        port = {free_port()}

        [review]
        host = "gerrit"

        [disk]
        caches = ["dist"]
    """
    LINE = "you may run kendle stack commands (start, stop, restart, wait, status, logs) for its own services"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.w.push_ref({"a.txt": "1"}, "Cart - first try", "refs/changes/11/1111/1")
        cls.w.push_ref({"b.txt": "1"}, "Menu - first try", "refs/changes/22/2222/1")

    def tearDown(self):
        for row in self.w.kendle("reviews").stdout.splitlines():
            self.w.kendle("review-close", row.split()[1].split("/")[0], check=False)
        for n in (1, 2, 3):
            self.w.kendle("stack", "stop", "-f", f"review-{n}", check=False)

    def open_review(self, change):
        out = self.w.kendle("review", change).stdout
        folder = out.rsplit(" in ", 1)[1].strip()
        return folder, os.path.basename(folder)

    def state(self, name):
        """By name: the folder may be gone."""
        rows = self.w.py(f"from kendle import stack; print(json.dumps([s['state'] for s in stack.status({name!r})]))")
        return rows[0] if rows else None

    def start_stack(self, folder):
        self.w.kendle("stack", "start", "web", cwd=folder)
        self.assertEqual(self.w.kendle("stack", "wait", cwd=folder).returncode, 0)

    def test_new_and_returning_reviews_carry_the_rule_and_the_line(self):
        folder, _ = self.open_review("1111")
        sid = next(e for e in self.w.py("from kendle import core; print(json.dumps(core.reviews()))") if e["change"] == "1111")["id"]
        args = wait_for(lambda: [c["args"] for c in self.w.claude_calls() if sid in c["args"]])[0]
        self.assertEqual(args[args.index("--allowedTools") + 1], "Bash(kendle stack *)")
        self.assertIn(self.LINE, "\n".join(args[args.index("--append-system-prompt") + 1:]))   # one argument, many lines
        self.w.write_transcript(self.w.transcript_path(sid, folder), [user("review it"), assistant("Approve")])
        self.w.kendle("stop", sid)
        self.open_review("1111")
        args = wait_for(lambda: [c["args"] for c in self.w.claude_calls() if "--resume" in c["args"] and sid in c["args"]])[0]
        self.assertTrue(args[0].startswith("I am back - "), args[0])
        self.assertIn(self.LINE, args[0])
        self.assertEqual(args[args.index("--allowedTools") + 1], "Bash(kendle stack *)")

    def test_closing_a_review_stops_its_stack(self):
        folder, name = self.open_review("2222")
        self.start_stack(folder)
        self.w.kendle("review-close", "2222")
        self.assertEqual(self.state(name), "stopped")
        self.assertIn("stopped by kendle stack - review closed", read(os.path.join(self.w.state, "stack", name, "web.log")))
        self.assertFalse(os.path.exists(folder))

    def test_a_new_revision_in_the_same_folder_stops_the_stack_first(self):
        first = self.w.push_ref({"c.txt": "1"}, "Search - first try", "refs/changes/33/3333/1")
        folder, name = self.open_review("3333")
        sid = next(e for e in self.w.py("from kendle import core; print(json.dumps(core.reviews()))") if e["change"] == "3333")["id"]
        self.w.write_transcript(self.w.transcript_path(sid, folder), [user("review it"), assistant("Comments")])
        self.w.kendle("stop", sid)
        self.start_stack(folder)
        self.open_review("3333")                               # the same revision: nothing moves, it keeps running
        self.assertEqual(self.state(name), "up")
        self.w.kendle("stop", sid)
        self.w.push_ref({"c.txt": "2"}, "Search - second try", "refs/changes/33/3333/2", parent=first)
        again, _ = self.open_review("3333")
        self.assertEqual(again, folder)
        self.assertEqual(read(os.path.join(folder, "c.txt")), "2")
        self.assertEqual(self.state(name), "stopped")
        self.assertIn("moved to a new revision", read(os.path.join(self.w.state, "stack", name, "web.log")))

    def test_a_folder_taken_by_another_change_stops_the_old_stack_first(self):
        self.w.push_ref({"d.txt": "1"}, "Login - first try", "refs/changes/44/4444/1")
        self.w.push_ref({"e.txt": "1"}, "Logout - first try", "refs/changes/55/5555/1")
        folder, name = self.open_review("4444")
        sid = next(e for e in self.w.py("from kendle import core; print(json.dumps(core.reviews()))") if e["change"] == "4444")["id"]
        self.start_stack(folder)
        self.w.kendle("stop", sid)                             # its session ends: the folder is free again
        self.assertEqual(self.state(name), "up")               # stopping the session alone keeps the stack
        again, _ = self.open_review("5555")
        self.assertEqual(again, folder)                        # the same folder, another change
        self.assertTrue(os.path.exists(os.path.join(folder, "e.txt")))
        self.assertEqual(self.state(name), "stopped")
        self.assertIn("stopped by kendle stack - review closed", read(os.path.join(self.w.state, "stack", name, "web.log")))


    def test_idle_stop_and_disk_trim_count_a_review_or_its_sub_agent_mid_turn_as_activity(self):
        self.w.push_ref({"f.txt": "1"}, "Prices - first try", "refs/changes/66/6666/1")
        folder, name = self.open_review("6666")
        sid = next(e for e in self.w.py("from kendle import core; print(json.dumps(core.reviews()))") if e["change"] == "6666")["id"]
        path = self.w.transcript_path(sid, folder)
        sub = os.path.join(path[:-6], "subagents", "agent-a1.jsonl")
        os.makedirs(os.path.dirname(sub))
        dist = os.path.join(folder, "dist")
        os.makedirs(dist)
        with open(os.path.join(dist, "bundle.js"), "w") as f:
            f.write("x" * 1024)
        idle = lambda: self.w.py("""
            from kendle import stack
            print(json.dumps(stack.stop_idle(minutes=1 / 60)))""")
        trim = lambda: self.w.kendle("disk", "trim", name).stdout
        busy = f"skipped {name}: it is working right now or its services are up"
        # the review itself is mid-turn: its stack runs on, and its caches are kept even without a stack
        self.w.write_transcript(path, [user("review it"), assistant(stop="tool_use", tool=("Bash", {}))])
        self.assertIn(busy, trim())
        self.start_stack(folder)
        time.sleep(1.1)
        self.assertEqual(idle(), [])
        # the review waits on an in-process sub-agent that is mid-tool-call: still activity
        self.w.write_transcript(path, [user("review it", ago=60), assistant("asking a sub-agent", ago=60)])
        self.w.write_transcript(sub, [user("check the tests"), assistant(stop="tool_use", tool=("Bash", {}))])
        self.assertEqual(idle(), [])
        self.assertEqual(self.state(name), "up")
        # both quiet for a minute: the stack stops as idle, and then the caches may go
        self.w.write_transcript(sub, [user("check the tests", ago=60), assistant("all pass", ago=60)])
        self.assertEqual(idle(), [name])
        self.assertIn("idle for", read(os.path.join(self.w.state, "stack", name, "web.log")))
        self.assertIn("  dist", trim())
        self.assertFalse(os.path.exists(dist))

if __name__ == "__main__":
    unittest.main()
