"""kendle view - what the console's right-hand pane shows when it is not a live session.

  kendle view idle                               placeholder with the key map
  kendle view transcript <file> <title> [desc]   read-only rendered chat of an internal
                                                 sub-agent, following new events
"""
import json, signal, sys, time

BOLD, DIM, CYAN, OFF = "\033[1m", "\033[2m", "\033[36m", "\033[0m"
KEYS = """
  kendle console

  j k  or click     select             Enter       show it here
  Tab  or click     type into it       Ctrl-q      sidebar ⇄ session

  a  ask a question on the latest base      p  promote it to a feature
  g  review someone else's change (up to 3, read-only)
  n  new feature    m  manager         s  read-only sub-agent
  K  stop session   r  refresh         q  close the console
  < >  narrower / wider sidebar (or drag the border - it is remembered)
"""
ARG_KEYS = ("command", "file_path", "pattern", "path", "url", "query", "description", "prompt")


def idle():
    def draw(*_):
        sys.stdout.write("\033[2J\033[H\033[?25l" + DIM + KEYS + OFF)
        sys.stdout.flush()
    signal.signal(signal.SIGWINCH, draw)
    draw()
    while True:
        signal.pause()


def render(e):
    kind, msg = e.get("type"), e.get("message") or {}
    content = msg.get("content")
    if kind == "user" and not e.get("isMeta"):
        if isinstance(content, list):
            if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
                return ""
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        text = (content or "").strip()
        return "" if not text or text.startswith("<") else f"\n{CYAN}{BOLD}❯{OFF} {text}\n"
    if kind == "assistant":
        out = []
        for c in content or []:
            if c.get("type") == "text" and c.get("text", "").strip():
                out.append(f"\n{BOLD}⏺{OFF} {c['text'].strip()}\n")
            elif c.get("type") == "tool_use":
                args = c.get("input") or {}
                arg = next((str(args[k]) for k in ARG_KEYS if k in args), "")
                out.append(f"{DIM}  ⎿ {c.get('name')}  {arg.replace(chr(10), ' ')[:100]}{OFF}\n")
        return "".join(out)
    return ""


def transcript(path, title, description=""):
    sys.stdout.write(f"\033[2J\033[H\033[?25l{BOLD}{title}{OFF}  {DIM}{description}{OFF}\n"
                     f"{DIM}read-only transcript · prefix h back to the sidebar{OFF}\n")
    pos, rest = 0, b""
    while True:
        try:
            with open(path, "rb") as f:
                f.seek(pos)
                data = f.read()
                pos = f.tell()
        except OSError:
            data = b""
        if data:
            *lines, rest = (rest + data).split(b"\n")
            for line in lines:
                try:
                    sys.stdout.write(render(json.loads(line)))
                except (ValueError, AttributeError):
                    pass
            sys.stdout.flush()
        time.sleep(1)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.SIG_IGN)     # Ctrl-C here must not empty the slot
    if sys.argv[1:] == ["idle"]:
        idle()
    elif len(sys.argv) in (4, 5) and sys.argv[1] == "transcript":
        transcript(*sys.argv[2:])
    print(__doc__.strip(), file=sys.stderr)
    sys.exit(2)
