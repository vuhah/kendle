"""kendle - start, show and stop kendle console sessions from a shell.

  kendle list [--json]                         features > manager > sub-agents, with state
  kendle new <feature>                         worktree in the workspace, from the fetched base
  kendle manager <feature> [-- claude args]    start the feature's manager, or resume it
  kendle sub <manager> <name> "<prompt>" [-- claude args]
                                               spawn a read-only sub-agent under a manager
  kendle show <session>                        put a session in the console's right pane
  kendle stop <session>                        stop a session (never touches the worktree)
  kendle adopt <feature> <session-id>          register an existing session as the manager
  kendle fresh <feature> ["<first message>"]   stop its manager and start a new one from the docs
  kendle ask "<question>" [-- claude args]     ask on the Ask desk (latest base, read-only)
  kendle ask --all                             every question, answered and closed too
  kendle promote <question> <feature>          turn a question into a feature with its manager
  kendle review <change|url>                   review someone else's change (read-only, Gerrit/GitHub/GitLab)
  kendle reviews                               the open reviews
  kendle review-close <change|id>              close a review: keep the findings, free the folder

<manager> is a feature name. <session> is a session id or unique prefix, a feature
name (its manager), or <feature>/<sub-name>.
"""
import json, os, sys
from kendle import core

DIM, BOLD, OFF = "\033[2m", "\033[1m", "\033[0m"


def print_tree(tree):
    for f in tree:
        where = "" if f["inside_hub"] else f"  {DIM}outside the hub: {f['path']}{OFF}"
        print(f"{BOLD}{f['name']}{OFF}  {DIM}[{f['branch']}]{OFF}{where}")
        if not f["managers"]:
            print(f"  {DIM}no manager{OFF}")
        for m in f["managers"]:
            print(f"  ● {'manager':<16} {m['state']:<8} {DIM}{m['id'][:8]}{OFF}")
            for s in m["subs"]:
                print(f"    ◦ {s['name'][:14]:<14} {s['state']:<8} {DIM}{s['id'][:8]}  read-only{OFF}")
            for s in m["internal"]:
                print(f"    ◦ {s['name'][:14]:<14} {s['state']:<8} {DIM}internal{OFF}")


def print_questions(qs, d):
    if d:
        print(f"{BOLD}ASK{OFF}  {DIM}{core.BASE} @ {d['sha']} ({d['date']}){' ' + str(d['behind']) + ' behind' if d['behind'] else ''}{OFF}")
    for q in qs:
        print(f"  ● {q['name'][:44]:<44} {q['state']:<14} {DIM}{q['id'][:8]}{OFF}")


def main(argv):
    extra = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "list" and args in ([], ["--json"]):
            tree = core.tree()
            if args:
                print(json.dumps({"ask": core.questions(), "features": tree}, indent=2))
            else:
                print_questions(core.questions(), core.desk())
                print_tree(tree)
        elif cmd == "new" and len(args) == 1:
            f = core.new_feature(args[0])
            print("created", f["name"], "at", f["path"], "on branch", f["branch"])
        elif cmd == "manager" and len(args) == 1:
            e, started = core.start_manager(args[0], extra)
            print("started" if started else "already running", e["id"], e["pane"])
        elif cmd == "sub" and len(args) == 3:
            e = core.spawn_sub(args[0], args[1], args[2], extra)
            print("started", e["id"], e["pane"])
        elif cmd == "show" and len(args) == 1:
            e = core.show(args[0])
            print("showing", core.label(e), e["id"])
        elif cmd == "stop" and len(args) == 1:
            e = core.stop(args[0])
            print("stopped", core.label(e), e["id"])
        elif cmd == "ask" and args == ["--all"]:
            print_questions(core.questions(all_=True), core.desk())
        elif cmd == "ask" and len(args) == 1:
            e = core.start_question(args[0], extra)
            print("asking", e["id"], e["pane"], "- kendle show", e["id"][:8])
        elif cmd == "promote" and len(args) == 2:
            f, m = core.promote(args[0], args[1])
            print("promoted to", f["name"], "at", f["path"], "- manager", m["id"])
        elif cmd == "fresh" and len(args) in (1, 2):
            for e in core.load():
                if e["feature"] == args[0] and e["kind"] == "manager" and e.get("pane"):
                    core.stop(e["id"])
            name = args[0]
            first = args[1] if len(args) == 2 else core.note("fresh", feature=name)
            m, _ = core.start_manager(name, extra, first, fresh=True)
            print("fresh manager", m["id"], m["pane"])
        elif cmd == "review" and len(args) == 1:
            e = core.start_review(args[0], extra)
            print(f"review {e['change']}/{e['patchset']} '{e['subject']}' by {e['author']} in {e['cwd']}")
        elif cmd == "reviews":
            for r in core.reviews(all_=bool(args)):
                print(f"  {r['state']:<10} {r['change']}/{r['patchset']:<3} {r['subject'][:48]:<48} {r['author']}")
        elif cmd == "review-close" and len(args) == 1:
            e = core.release_review(args[0])
            print("closed review", e["change"], "- findings:", e.get("saved") or "(nothing to save)")
        elif cmd == "adopt" and len(args) == 2:
            e = core.adopt(args[0], args[1])
            print("adopted", e["id"], "as the", e["feature"], "manager")
        else:
            print(__doc__.strip(), file=sys.stderr)
            return 2
    except (LookupError, ValueError, RuntimeError) as err:
        print("kendle:", err, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
