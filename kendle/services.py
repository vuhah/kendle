"""Where a feature's services come from: kendle.toml's [services], in one of three forms.

  [services.api]                       a plain list: a shell command, run in the worktree
  run = "npm start"
  cwd = "web"
  port = 3000

  [services.api]                       an IntelliJ run configuration (Bazel run, or npm)
  intellij = ["Bazel_run_api.xml"]     the first one that exists; run/env/port come from it

  [services.db]                        a docker compose service, from the worktree's compose file
  compose = "db"                       (to run in the second stack too, give it its port here and
  port = 5432                           publish it as "${KENDLE_PORT:-5432}:5432" in the compose file)

A service with only a port is check-only: `kendle services` reports it, kendle never starts it.
Every form turns into the same spec, which kendle.stack runs: kendle owns the process group it starts
and never touches a process it did not start.
"""
import json, os, re, shlex, subprocess
import xml.etree.ElementTree as ET
from kendle import config, core

SETTINGS = {
    "shift_by": 100,             # the second stack runs with every shifted port moved by this much
    "shift_ports": None,         # ports that move in the second stack; default: those of the services kendle starts
    "hosts": ["localhost", "127.0.0.1"],   # each stack's host - browser cookies are per host
    "app": None,                 # the service the browser opens; its URL is shown
    "idle_minutes": 30,          # stop a feature's services after its sessions are quiet this long
    "intellij_dir": ".idea/runConfigurations",   # relative to the worktree; may start with {workspace}
    "compose_file": "docker-compose.yml",        # relative to the worktree
}
SERVICE_KEYS = {"run", "run_shifted", "cwd", "port", "env", "intellij", "node_fallback", "compose",
                "watch", "watch_names", "shift", "up_when"}
PORT_FLAGS = ("server.port", "spring.grpc.server.port", "grpc.port", "port")


def _load():
    raw = dict(core.CONFIG.get("services") or {})
    settings = {k: raw.pop(k) for k in list(raw) if k in SETTINGS}
    services = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            raise SystemExit(f"kendle: kendle.toml: services.{name} must be a table")
        unknown = set(spec) - SERVICE_KEYS
        if unknown:
            raise SystemExit(f"kendle: kendle.toml: unknown key services.{name}.{sorted(unknown)[0]}")
        services[name] = spec
    return dict(SETTINGS, **settings), services


SETTINGS, SERVICES = _load()


def _ports(values):
    out = set()
    for v in values:
        if isinstance(v, int):
            out.add(v)
        else:
            lo, _, hi = str(v).partition("-")
            out |= set(range(int(lo), int(hi or lo) + 1))
    return out


def startable(name):
    s = SERVICES[name]
    return bool(s.get("run") or s.get("intellij") or "compose" in s)


# default: the ports of the services kendle starts - never a check-only one (a shared database stays put)
SHIFTABLE = _ports(SETTINGS["shift_ports"]) if SETTINGS["shift_ports"] is not None else \
            {s["port"] for n, s in SERVICES.items() if isinstance(s.get("port"), int) and startable(n)}


def offset(slot):
    return SETTINGS["shift_by"] * slot


def host(slot):
    hosts = SETTINGS["hosts"]
    return hosts[min(slot, len(hosts) - 1)]


# ---- shifting ports for the second stack ----------------------------------------------

def shift_text(text, off, host_only=None):
    """host:port references to a shifted port, moved by `off` (and onto host_only, if given)."""
    def move(m):
        port = int(m.group(2))
        return f"{host_only or m.group(1)}:{port + off}" if port in SHIFTABLE else m.group(0)
    return re.sub(r"\b(localhost|127\.0\.0\.1):(\d{2,5})\b", move, text) if off else text


def shift_args(args, off):
    """Every port a service listens on or calls, moved by the slot's offset; never a port that is
    not shiftable (a shared database stays where it is)."""
    if not off:
        return list(args)
    out = []
    for a in args:
        if a.startswith("--") and "=" in a:
            key, val = a[2:].split("=", 1)
            if key.lower().endswith("port") and val.isdigit() and int(val) in SHIFTABLE:
                val = str(int(val) + off)
            else:
                val = shift_text(val, off)
            a = f"--{key}={val}"
        out.append(a)
    return out


# ---- the three sources ---------------------------------------------------------------

def _intellij_dir(worktree):
    d = config.fill(SETTINGS["intellij_dir"], workspace=core.HUB, worktree=worktree)
    return d if os.path.isabs(d) else os.path.join(worktree, d)


def _intellij(name, spec, worktree):
    """The first of the service's IntelliJ run configurations that exists, read into a spec."""
    d = _intellij_dir(worktree)
    for fname in spec["intellij"]:
        path = os.path.join(d, fname)
        if not os.path.exists(path):
            continue
        cfg = next(ET.parse(path).iter("configuration"))
        if cfg.get("type") == "js.build_tools.npm":
            pkg = next((e.get("value") for e in cfg.iter("package-json")), "") or ""
            cwd = os.path.dirname(pkg.replace("$PROJECT_DIR$", worktree)) or worktree
            command = next((e.get("value") for e in cfg.iter("command")), "run")
            scripts = [e.get("value") for e in cfg.iter("script") if e.get("value")]
            node = next((e.get("value") for e in cfg.iter("node-interpreter")), "") or ""
            env = {e.get("name"): e.get("value", "") for e in cfg.iter("env") if e.get("name")}
            # the Node the run configuration uses; else the service's own fallback (e.g. an nvm version)
            use = f'export PATH="{os.path.dirname(node)}:$PATH"' if os.path.exists(node) else \
                  (spec.get("node_fallback") or "true")
            npm = "npm start" if (command, scripts) == ("run", ["start"]) else f"npm {command} {' '.join(scripts)}"
            run = f"{use} && ([ -d node_modules ] || npm ci --prefer-offline) && exec {npm}"
            return {"label": fname, "shell": run.strip(), "cwd": cwd, "env": env, "port": None}
        target = next((t.text.strip() for t in cfg.iter("bsp-target") if t.text), None)
        if not target:
            raise LookupError(f"{fname}: only Bazel run and npm run configurations are understood")
        handler = next(cfg.iter("handler-state"), None)
        args = shlex.split(handler.get("programArguments", "")) if handler is not None else []
        env = {e.get("key"): e.get("value", "") for e in cfg.iter("env") if e.get("key")}
        flags = dict(a[2:].split("=", 1) for a in args if a.startswith("--") and "=" in a)
        port = next((int(flags[k]) for k in PORT_FLAGS if k in flags and flags[k].isdigit()), None)
        return {"label": fname, "argv": ["bazel", "run", target, "--"], "args": args, "cwd": worktree,
                "env": env, "port": port}
    raise LookupError(f"no IntelliJ run configuration found for {name} in {d}")


def _compose_file(worktree):
    return os.path.join(worktree, SETTINGS["compose_file"])


def _compose(name, spec, worktree, feature, env=None):
    """The service as compose runs it; its port is the first one it publishes, with ${...} in the
    compose file filled from the environment plus env."""
    svc = spec.get("compose") or name
    base = ["docker", "compose", "-f", _compose_file(worktree), "-p", f"kendle-{feature}"]
    port = None
    r = subprocess.run(base + ["config", "--format", "json"], capture_output=True, text=True, cwd=worktree,
                       env=dict(os.environ, **(env or {})))
    if r.returncode == 0:
        for p in (json.loads(r.stdout).get("services", {}).get(svc, {}).get("ports") or []):
            if str(p.get("published", "")).isdigit():
                port = int(p["published"])
                break
    return {"label": f"compose:{svc}", "argv": base + ["up", "--no-log-prefix", svc], "args": [], "cwd": worktree,
            "env": {}, "port": port, "stop": base + ["stop", svc], "up_when": "listening"}


def spec(name, f, slot=0):
    """How to run one service for feature f in a stack slot: argv, cwd, env, port and more."""
    s = SERVICES.get(name)
    if s is None:
        raise LookupError(f"unknown service '{name}' - known: {', '.join(SERVICES) or 'none (kendle.toml [services])'}")
    if not startable(name):
        raise LookupError(f"{name} is check-only (kendle.toml gives it no run, intellij or compose)")
    off, worktree = offset(slot), f["path"]
    if s.get("intellij"):
        base = _intellij(name, s, worktree)
    elif "compose" in s:
        base = _compose(name, s, worktree, f["name"])
    else:
        base = {"label": "kendle.toml", "shell": s["run"], "port": None}
    port = s.get("port") or base.get("port")
    shifted_port = port + off if port and port in SHIFTABLE else port
    if off and port and "compose" in s and not s.get("intellij"):
        # the compose file decides what is published: it must take the second stack's port from kendle
        moved = _compose(name, s, worktree, f["name"], {"KENDLE_PORT": str(shifted_port)})["port"]
        if shifted_port == port or moved != shifted_port:
            raise LookupError(f"{name} would publish port {moved or port} in the second stack, as in the first - "
                              f"give it its port in kendle.toml (services.{name}: port = {port}) and publish that "
                              f"in the compose file as \"${{KENDLE_PORT:-{port}}}:<container port>\"")
    state = os.path.join(core.STATE, "stack", f["name"])
    fields = {"port": shifted_port or "", "host": host(slot), "offset": off, "shifted_dir": state,
              "worktree": worktree, "workspace": core.HUB, "feature": f["name"]}
    cwd = os.path.join(worktree, config.fill(s.get("cwd", ""), **fields)) if s.get("cwd") else base.get("cwd", worktree)
    if off:                                       # the second stack: shifted copies of the files it names
        os.makedirs(state, exist_ok=True)
        for rel in s.get("shift", []):
            with open(os.path.join(cwd, rel)) as src, open(os.path.join(state, os.path.basename(rel)), "w") as dst:
                dst.write(shift_text(src.read(), off, host_only="localhost"))
    shell = base.get("shell")
    if off and s.get("run_shifted"):
        prefix = shell.rsplit("exec ", 1)[0] if s.get("intellij") and "exec " in (shell or "") else ""
        shell = prefix + "exec " + config.fill(s["run_shifted"], **fields)
    elif shell is not None and not s.get("intellij"):
        shell = config.fill(shell, **fields)
    argv = ["/bin/bash", "-c", shell] if shell is not None else base["argv"] + shift_args(base["args"], off)
    env = core.clean_env()
    if not env.get("NVM_DIR") and os.path.exists(os.path.expanduser("~/.nvm/nvm.sh")):
        env["NVM_DIR"] = os.path.expanduser("~/.nvm")    # agent shells don't inherit it
    env.update(base.get("env") or {})
    env.update({k: str(v) for k, v in (s.get("env") or {}).items()})
    env.update(KENDLE_FEATURE=f["name"], KENDLE_PORT_OFFSET=str(off), KENDLE_HOST=host(slot))
    if shifted_port:
        env["KENDLE_PORT"] = str(shifted_port)
    return {"argv": argv, "cwd": cwd, "env": env, "port": shifted_port, "label": base["label"],
            "stop": base.get("stop"), "up_when": s.get("up_when") or base.get("up_when") or "ours"}


def describe(name, worktree):
    """One line about a service for `kendle stack services`: its port and where it comes from."""
    s = SERVICES[name]
    if not startable(name):
        return s.get("port"), "check-only"
    try:
        if s.get("intellij"):
            b = _intellij(name, s, worktree)
            return s.get("port") or b["port"], b["label"]
    except LookupError as err:
        return s.get("port"), f"({err})"
    if "compose" in s:
        port = s.get("port") or _compose(name, s, worktree, os.path.basename(worktree))["port"]
        return port, f"compose:{s.get('compose') or name}"
    return s.get("port"), s["run"]


def affects(name, path):
    """Whether a changed file means the running service must restart (restart-affected)."""
    s = SERVICES[name]
    names = set(s.get("watch_names") or [])
    if names and os.path.basename(path) not in names:
        return False
    return any(path.startswith(w) for w in s.get("watch") or [])


def app_url(slot):
    app = SETTINGS["app"]
    port = SERVICES.get(app, {}).get("port") if app else None
    if not port:
        return None
    return f"http://{host(slot)}:{port + offset(slot) if port in SHIFTABLE else port}"
