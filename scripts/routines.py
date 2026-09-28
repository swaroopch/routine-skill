#!/usr/bin/env python3
"""routines.py — markdown-defined scheduled opencode routines (launchd + herdr).

One markdown file per routine in routines/: frontmatter is config, body is the
prompt. `install` compiles each enabled routine into a LaunchAgent plist whose
only job is to re-invoke this script with `run <file>` at the scheduled time.
`run` reads the markdown at fire time, so prompt edits need no reinstall.

Run with: uv run --no-project python routines.py <command>
"""
from __future__ import annotations

import fcntl
import json
import os
import plistlib
import re
import signal
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve()


def configured_path(variable: str, default: Path) -> Path:
    return Path(os.environ.get(variable, str(default))).expanduser().resolve()


ROUTINES_DIR = configured_path("ROUTINES_DIR", Path.home() / ".config/routine/routines")
DATA_DIR = configured_path("ROUTINES_DATA_DIR", Path.home() / ".local/state/routine")
LOG_DIR = configured_path("ROUTINES_LOG_DIR", DATA_DIR / "logs")
LAUNCH_DIR = configured_path("ROUTINES_LAUNCH_DIR", Path.home() / "Library/LaunchAgents")
PROMPT_DIR = DATA_DIR / "prompts"
HERDR_LOCK = DATA_DIR / "herdr.lock"
LABEL_PREFIX = "com.routines"
WORKSPACE_LABEL = "routines"
UV = shutil.which("uv") or "uv"
EXECUTION_CONTEXT = """\
You are executing an already-installed routine. Perform the task below now.
Do not load routine-management skills or inspect, create, update, reinstall, or
troubleshoot the routine definition or schedule unless the task explicitly asks
you to manage routines."""

DAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


class RoutineError(Exception):
    pass


def slugify(text: str) -> str:
    return re.sub(r"-+$|^-+", "", re.sub(r"[^a-z0-9]+", "-", text.lower()))


def frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fm = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            key, _, value = line.partition(":")
            fm[key.strip()] = value.strip().strip("'\"")
    return fm


def body_of(text: str) -> str:
    parts = text.split("---")
    # text starts with '---\n...\n---\n<body>'; split gives ['', '\n...\n', body]
    if len(parts) >= 3:
        return "\n".join(parts[2:]).strip()
    return ""


def execution_prompt(body: str) -> str:
    return f"{EXECUTION_CONTEXT}\n\n{body}\n"


class Routine:
    def __init__(self, path: Path):
        self.path = path
        text = path.read_text()
        fm = frontmatter(text)
        self.name = fm.get("name") or path.stem
        self.slug = slugify(self.name)
        self.schedule = fm.get("schedule", "")
        self.model = fm.get("model", "")
        self.variant = fm.get("variant", "")
        self.agent = fm.get("agent", "")
        self.dir = os.path.expanduser(fm.get("dir", "")) if fm.get("dir") else ""
        self.auto = fm.get("auto", "false").lower() == "true"
        self.notify = fm.get("notify", "true").lower() != "false"
        self.enabled = fm.get("enabled", "true").lower() == "true"
        self.timeout = fm.get("timeout", "")
        self.timeout_seconds = parse_timeout(self.timeout)
        self.model_timeouts = {}
        for field, option in (
            ("model_header_timeout", "headerTimeout"),
            ("model_chunk_timeout", "chunkTimeout"),
            ("model_request_timeout", "timeout"),
        ):
            try:
                seconds = parse_timeout(fm.get(field, ""))
            except RoutineError as exc:
                raise RoutineError(f"{field}: {exc}") from exc
            if seconds is not None:
                self.model_timeouts[option] = seconds * 1000
        if self.model_timeouts and ("/" not in self.model or not all(self.model.split("/", 1))):
            raise RoutineError("model timeouts require an explicit provider/model")
        self.body = body_of(text)

    @property
    def label(self) -> str:
        return f"{LABEL_PREFIX}.{self.slug}"

    @property
    def plist_path(self) -> Path:
        return LAUNCH_DIR / f"{self.label}.plist"

    @property
    def log_path(self) -> Path:
        return LOG_DIR / f"{self.slug}.log"


def resolve_file(name_or_path: str) -> Path:
    path = Path(name_or_path)
    if not path.is_file():
        path = ROUTINES_DIR / f"{slugify(name_or_path)}.md"
    if not path.is_file():
        raise RoutineError(f"no routine found for '{name_or_path}' (looked in {ROUTINES_DIR})")
    return path.resolve()


def parse_schedule(s: str) -> list[tuple[int | None, int, int | None]]:
    """Returns (hour, minute, weekday) triples; None means 'every'."""
    s = s.strip()
    time_match = re.search(r"(\d{1,2})?:(\d{2})", s)
    hour = int(time_match.group(1)) if time_match and time_match.group(1) else None
    minute = int(time_match.group(2)) if time_match else None

    if s.startswith("hourly"):
        if minute is None:
            raise RoutineError("hourly needs a ':MM' time, e.g. 'hourly :15'")
        return [(None, minute, None)]
    if minute is None or hour is None:
        raise RoutineError(f"no HH:MM time found in schedule '{s}'")
    if s.startswith("daily"):
        return [(hour, minute, None)]
    if s.startswith("weekly"):
        parts = s.split()
        if len(parts) < 2:
            raise RoutineError("weekly needs a day name, e.g. 'weekly Mon 10:00' or 'weekly Mon,Thu 10:00'")
        out = []
        for day in parts[1].split(","):
            if day[:3].lower() not in DAYS:
                raise RoutineError(f"unknown day '{day}' in schedule '{s}'")
            out.append((hour, minute, DAYS[day[:3].lower()]))
        return out
    range_match = re.match(r"^([A-Za-z]{3})-([A-Za-z]{3})\b", s)
    if range_match:
        a, b = DAYS.get(range_match.group(1).lower()), DAYS.get(range_match.group(2).lower())
        if a is None or b is None:
            raise RoutineError(f"unknown day in schedule '{s}'")
        if a > b:
            raise RoutineError("day range must ascend, e.g. Mon-Fri")
        return [(hour, minute, d) for d in range(a, b + 1)]
    raise RoutineError(
        f"unrecognized schedule '{s}' — use: daily HH:MM | Mon-Fri HH:MM | weekly Mon HH:MM | hourly :MM"
    )


def parse_timeout(value: str) -> int | None:
    if not value:
        return None
    match = re.fullmatch(r"([1-9]\d*)\s*([smh]?)", value.strip().lower())
    if not match:
        raise RoutineError(f"invalid timeout '{value}' — use seconds or a suffix such as 45m or 2h")
    amount = int(match.group(1))
    multiplier = {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]
    return amount * multiplier


def launchctl(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    if sys.platform != "darwin":
        raise RoutineError("scheduling requires macOS launchd")
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=check)


def runtime_env(r: Routine | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if r is not None and r.model_timeouts:
        # Compatibility verified only with OpenCode 1.18.31 (2026-09-21).
        # On upgrades, especially v2, recheck config merge/timeout semantics and
        # fault-test header stalls, chunk stalls, and heartbeat-only streams.
        # In 1.18.31 header/chunk stalls retry; full-request timeouts are terminal.
        # Unit tests below this skill cover launcher wiring, not OpenCode behavior.
        try:
            config = json.loads(env.get("OPENCODE_CONFIG_CONTENT", "{}"))
        except json.JSONDecodeError as exc:
            raise RoutineError("OPENCODE_CONFIG_CONTENT must be valid JSON") from exc
        if not isinstance(config, dict):
            raise RoutineError("OPENCODE_CONFIG_CONTENT must be a JSON object")
        provider_id = r.model.split("/", 1)[0]
        providers = config.setdefault("provider", {})
        if not isinstance(providers, dict):
            raise RoutineError("OPENCODE_CONFIG_CONTENT.provider must be an object")
        provider = providers.setdefault(provider_id, {})
        if not isinstance(provider, dict):
            raise RoutineError(f"provider {provider_id} must be an object")
        options = provider.setdefault("options", {})
        if not isinstance(options, dict):
            raise RoutineError(f"provider {provider_id} options must be an object")
        options.update(r.model_timeouts)
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    return env


def bootout(label: str) -> None:
    launchctl(["bootout", f"gui/{os.getuid()}/{label}"], check=False)


def install_one(r: Routine) -> None:
    LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not r.enabled:
        bootout(r.label)
        r.plist_path.unlink(missing_ok=True)
        print(f"disabled: {r.slug} (plist removed, markdown kept)")
        return
    if not r.schedule:
        raise RoutineError(f"{r.path}: frontmatter has no schedule")

    intervals = []
    for hour, minute, weekday in parse_schedule(r.schedule):
        interval = {"Minute": minute}
        if hour is not None:
            interval["Hour"] = hour
        if weekday is not None:
            interval["Weekday"] = weekday
        intervals.append(interval)

    uv = shutil.which("uv")
    if not uv:
        raise RoutineError("uv is required and must be on PATH when installing")
    # Capture executable locations, not a user's shell config or credentials.
    tool_dirs = [
        str(Path(binary).parent)
        for tool in ("uv", "opencode", "herdr", "terminal-notifier")
        if (binary := shutil.which(tool))
    ]
    plist = {
        "Label": r.label,
        "ProgramArguments": [uv, "run", "--no-project", "python", str(SCRIPT_PATH), "run", str(r.path)],
        "WorkingDirectory": str(Path.home()),
        "StartCalendarInterval": intervals,
        "StandardOutPath": str(r.log_path),
        "StandardErrorPath": str(r.log_path),
        # launchd starts with a minimal environment. Persist explicit directory
        # overrides so a scheduled run uses the same storage as the installer.
        "EnvironmentVariables": {
            "PATH": os.pathsep.join(dict.fromkeys([*tool_dirs, "/usr/bin", "/bin", "/usr/sbin", "/sbin"])),
            "ROUTINES_DIR": str(ROUTINES_DIR),
            "ROUTINES_DATA_DIR": str(DATA_DIR),
            "ROUTINES_LOG_DIR": str(LOG_DIR),
            "ROUTINES_LAUNCH_DIR": str(LAUNCH_DIR),
        },
    }
    with open(r.plist_path, "wb") as f:
        plistlib.dump(plist, f)

    bootout(r.label)
    launchctl(["bootstrap", f"gui/{os.getuid()}", str(r.plist_path)])
    print(f"installed: {r.slug} — {r.schedule}")


def cmd_install(names: list[str]) -> None:
    files = [resolve_file(n) for n in names] if names else sorted(ROUTINES_DIR.glob("*.md"))
    if not files:
        raise RoutineError(f"no routines in {ROUTINES_DIR} yet — create one first")
    for f in files:
        install_one(Routine(f))


def cmd_create(name: str, schedule: str) -> None:
    parse_schedule(schedule)  # validate before writing anything
    ROUTINES_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = ROUTINES_DIR / f"{slugify(name)}.md"
    if path.exists():
        raise RoutineError(f"already exists: {path} — edit it, then: routines.py install {path.name}")
    path.write_text(
        f"""---
name: {name}
schedule: "{schedule}"
model:
variant:
agent:
dir:
auto: false
enabled: true
---

(Prompt body — what opencode should do on each run.)
"""
    )
    if sys.stdin.isatty() and os.environ.get("EDITOR"):
        subprocess.run([os.environ["EDITOR"], str(path)], check=True)
    install_one(Routine(path))
    print(f"routine file: {path}")


def cmd_remove(name: str, purge: bool = False) -> None:
    path = resolve_file(name)
    r = Routine(path)
    bootout(r.label)
    r.plist_path.unlink(missing_ok=True)
    print(f"unscheduled: {r.slug}")
    if purge:
        path.unlink()
        print(f"deleted: {path}")


def cmd_list() -> None:
    print(f"{'NAME':<30} {'SCHEDULE':<16} {'STATE':<14} MODEL")
    for path in sorted(ROUTINES_DIR.glob("*.md")):
        r = Routine(path)
        if r.enabled and r.plist_path.exists():
            state = "scheduled"
        elif r.enabled:
            state = "NOT-INSTALLED"
        else:
            state = "disabled"
        print(f"{r.name:<30} {r.schedule or '-':<16} {state:<14} {r.model or 'default'}")


def opencode_args(r: Routine) -> list[str]:
    args = ["run", "--title", r.name]
    if r.model:
        args += ["--model", r.model]
    if r.variant:
        args += ["--variant", r.variant]
    if r.agent:
        args += ["--agent", r.agent]
    if r.dir:
        args += ["--dir", r.dir]
    if r.auto:
        args.append("--auto")
    return args


def execute_opencode(r: Routine, prompt: Path) -> int:
    """Run OpenCode with an optional wall-clock limit and kill all children on timeout."""
    with open(prompt) as stdin:
        process = subprocess.Popen(
            ["opencode", *opencode_args(r)],
            stdin=stdin,
            env=runtime_env(r),
            start_new_session=True,
        )

    def forward_signal(signum, _frame) -> None:
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass

    previous_handlers = {
        signum: signal.signal(signum, forward_signal)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        try:
            return process.wait(timeout=r.timeout_seconds)
        except subprocess.TimeoutExpired:
            print(f"error: routine timed out after {r.timeout}", file=sys.stderr)
            forward_signal(signal.SIGTERM, None)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                forward_signal(signal.SIGKILL, None)
                process.wait()
            return 124
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def cmd_execute(name: str, prompt: str) -> int:
    """Internal entrypoint used by herdr to enforce a routine's timeout."""
    return execute_opencode(Routine(resolve_file(name)), Path(prompt))


def herdr_json(args: list[str]) -> dict:
    out = subprocess.run(["herdr", *args], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


NOTIFIER = shutil.which("terminal-notifier")


def notify(r: Routine, ok: bool, detail: str) -> None:
    """macOS notification that a routine finished, ChatGPT/Codex style.

    Prefers terminal-notifier (group coalescing, click-to-herdr); falls back
    to osascript if it's missing or blocked (e.g. notifications disabled for
    the app in System Settings). The banner icon is the notifier app's own —
    macOS gives senders no way to override it.
    """
    if not r.notify:
        return
    title = f"Routine {'finished' if ok else 'failed'}: {r.name}"
    subtitle = datetime.now().strftime("%a %H:%M")
    sent = False
    if NOTIFIER:
        args = [
            NOTIFIER,
            "-title", title,
            "-subtitle", subtitle,
            "-message", detail,
            "-group", r.label,
        ]
        if herdr_available():
            args += ["-execute", "open -a herdr"]
        try:
            sent = subprocess.run(args, capture_output=True, timeout=10, check=False).returncode == 0
        except (subprocess.SubprocessError, OSError):
            sent = False
    if not sent:
        detail = detail.replace("\\", "").replace('"', "")
        script = (
            f'display notification "{detail}" '
            f'with title "{title}" subtitle "{subtitle}"'
        )
        try:
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10, check=False)
        except (subprocess.SubprocessError, OSError):
            pass


def herdr_available() -> bool:
    if not shutil.which("herdr"):
        return False
    try:
        subprocess.run(["herdr", "workspace", "list"], capture_output=True, timeout=5, check=True)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


@contextmanager
def herdr_lock():
    """Serialize herdr workspace/tab allocation across simultaneous fires.

    Routines scheduled at the same minute run as independent launchd jobs. Without
    a lock, each one sees no 'routines' workspace and creates its own.
    """
    HERDR_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(HERDR_LOCK, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def pane_id_of(obj) -> str | None:
    if isinstance(obj, dict):
        return obj.get("pane_id") or obj.get("id")
    return obj


def session_id_of(obj) -> str | None:
    """Find the OpenCode session herdr recorded for a pane."""
    if isinstance(obj, dict):
        for key in ("agent_session_id", "session_id", "sessionID"):
            value = obj.get(key)
            if isinstance(value, str) and value.startswith("ses_"):
                return value
        for value in obj.values():
            session_id = session_id_of(value)
            if session_id:
                return session_id
    elif isinstance(obj, list):
        for value in obj:
            session_id = session_id_of(value)
            if session_id:
                return session_id
    elif isinstance(obj, str) and obj.startswith("ses_"):
        return obj
    return None


def opencode_bin() -> str:
    return shutil.which("opencode", path=runtime_env()["PATH"]) or "opencode"


def session_id_by_title(title: str, since_ms: int) -> str | None:
    """Newest opencode session titled `title` and created at/after `since_ms`.

    Every run is launched with `--title <routine name>`, so the routine's own
    session is identifiable from opencode's own records — no herdr involved.
    `since_ms` (the fire time) keeps earlier runs of the same routine out.
    """
    out = subprocess.run(
        [opencode_bin(), "session", "list", "--format", "json", "-n", "200"],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        sessions = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None
    matches = [
        s
        for s in sessions
        if isinstance(s, dict)
        and s.get("title") == title
        and int(s.get("created") or 0) >= since_ms
    ]
    if not matches:
        return None
    return max(matches, key=lambda s: int(s.get("created") or 0)).get("id")


def herdr_pane_session_id(pane: str) -> str | None:
    """Session herdr attributed to a pane; absent for panes whose agent exited."""
    try:
        return session_id_of(herdr_json(["pane", "get", pane]))
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None


def find_workspace() -> str | None:
    result = herdr_json(["workspace", "list"]).get("result", {})
    workspaces = result.get("workspaces", []) if isinstance(result, dict) else result
    return next(
        (
            w.get("workspace_id") or w.get("id")
            for w in workspaces
            if isinstance(w, dict) and w.get("label") == WORKSPACE_LABEL
        ),
        None,
    )


def routine_tab(r: Routine, tab_label: str) -> tuple[str | None, str | None]:
    """Return (tab_id, pane_id) of a fresh, idle tab for this run.

    A new workspace already ships with a root tab and pane, so the first routine
    of a herdr session renames and reuses that one. Creating another tab instead
    leaves an empty tab 1 behind and shifts every routine one slot down the list.
    """
    ws = find_workspace()
    if ws is None:
        cwd = r.dir if r.dir and Path(r.dir).is_dir() else str(Path.home())
        created = herdr_json(
            ["workspace", "create", "--cwd", cwd, "--label", WORKSPACE_LABEL]
        )["result"]
        root = created.get("root_pane") if isinstance(created, dict) else None
        tab_id = (created.get("tab") or {}).get("tab_id") or (root or {}).get("tab_id")
        if tab_id:
            herdr_json(["tab", "rename", str(tab_id), tab_label])
            return tab_id, pane_id_of(root)
        workspace = created.get("workspace") if isinstance(created, dict) else None
        ws = (workspace or created).get("workspace_id") or (workspace or created).get("id")

    created = herdr_json(["tab", "create", "--workspace", str(ws), "--label", tab_label])["result"]
    pane = created.get("root_pane") or created.get("pane") or (created.get("tab", {}) or {}).get("root_pane")
    tab_id = (created.get("tab") or {}).get("tab_id") or (pane or {}).get("tab_id")
    return tab_id, pane_id_of(pane)


def wait_for_shell(pane: str) -> None:
    """Block until the pane's shell has printed a prompt.

    `pane run` types into the pty, so firing it before the shell starts leaves the
    command sitting in the input buffer at the mercy of the shell's startup — it
    gets echoed twice at best and dropped at worst. Non-fatal: an unusual prompt
    should slow a routine down, not break it.
    """
    subprocess.run(
        [
            "herdr", "pane", "wait-output", str(pane),
            "--regex", r"\$", "--source", "visible", "--timeout", "8000",
        ],
        capture_output=True,
        check=False,
    )


def run_in_herdr(r: Routine, run_cmd: str, started_ms: int) -> None:
    tab_label = f"{r.name} · {datetime.now().strftime('%a %H:%M')}"
    with herdr_lock():
        tab_id, pane = routine_tab(r, tab_label)
        if not pane:
            raise RoutineError("herdr returned no pane id for the new routine tab")
        wait_for_shell(pane)
        if tab_id:
            # opencode's TUI rewrites the terminal title to its own generated
            # session title, and herdr syncs the tab label from that title — so
            # the routine name and fire time vanish from the sidebar. Re-assert
            # the label once the TUI has settled.
            relabel = shlex.join(["herdr", "tab", "rename", str(tab_id), tab_label])
            relabel_cmd = f"( sleep 8; {relabel} >/dev/null 2>&1 ) & "
        else:
            relabel_cmd = ""
        session_lookup = shlex.join(
            [
                UV, "run", "--no-project", "python", str(SCRIPT_PATH), "session-id",
                str(r.path), str(started_ms), str(pane),
            ]
        )
        resume_cmd = (
            f'SESSION_ID=$({session_lookup}); '
            'if [ -n "$SESSION_ID" ]; then '
            'opencode --session "$SESSION_ID"; '
            'else printf "Unable to reopen routine: no OpenCode session found for this run.\\n" >&2; fi'
        )
        cmd_string = f"{run_cmd}; {relabel_cmd}{resume_cmd}"
        subprocess.run(["herdr", "pane", "run", str(pane), cmd_string], check=True)
    print(
        f"running in herdr: workspace '{WORKSPACE_LABEL}', tab '{tab_label}' "
        f"({tab_id or 'unknown tab'}, pane {pane})"
    )


def cmd_notify_done(name: str, ok: bool) -> None:
    """Internal: fire the completion notification (used inside herdr panes)."""
    notify(Routine(resolve_file(name)), ok, "Done — session reopened in herdr for follow-ups")


def cmd_session_id(name: str, since_ms: str, pane: str | None = None) -> None:
    """Internal: print the session id of the run that just finished in this pane.

    opencode's session list is the primary source: the run titles its session
    after the routine, so title + fire time pins it exactly. herdr's pane
    record is only a fallback — it holds no session once the headless `run`
    process has exited, which is precisely when this lookup happens.
    """
    r = Routine(resolve_file(name))
    session_id = session_id_by_title(r.name, int(since_ms))
    if not session_id and pane:
        session_id = herdr_pane_session_id(pane)
    if not session_id:
        raise RoutineError(
            f"no opencode session titled '{r.name}' created since "
            f"{datetime.fromtimestamp(int(since_ms) / 1000):%H:%M:%S}"
            + (f", and herdr pane {pane} has none recorded" if pane else "")
        )
    print(session_id)


def cmd_run(name: str) -> None:
    r = Routine(resolve_file(name))
    if not r.enabled:
        print(f"skipped (enabled: false): {r.path}")
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    PROMPT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    prompt = PROMPT_DIR / f"{r.slug}.md"
    prompt.write_text(execution_prompt(r.body))

    started_ms = int(time.time() * 1000)
    if herdr_available():
        # Run the prompt one-shot (headless `run`), notify, then relaunch the
        # TUI with this run's session ID, resolved from opencode's session list
        # by title + fire time. Global --continue can select another
        # concurrently running routine.
        notify_cmd = shlex.join(
            [UV, "run", "--no-project", "python", str(SCRIPT_PATH), "notify-done", str(r.path)]
        )
        opencode_cmd = shlex.join(
            [UV, "run", "--no-project", "python", str(SCRIPT_PATH), "execute", str(r.path), str(prompt)]
        )
        run_cmd = (
            f"OK=0; {opencode_cmd} && OK=1; "
            f'{notify_cmd} "$OK"'
        )
        run_in_herdr(r, run_cmd, started_ms)
    else:
        print(f"herdr not reachable — running headless (logs: {r.log_path})")
        ok = True
        detail = f"Done — logs: {r.log_path}"
        if execute_opencode(r, prompt) != 0:
            ok = False
            detail = f"opencode exited nonzero — logs: {r.log_path}"
        notify(r, ok, detail)
        if not ok:
            raise SystemExit(1)


USAGE = """\
routines.py — markdown-defined scheduled opencode routines

  create <name> <schedule>   new routine md, validate schedule, install it
  install [name|file...]     (re)compile plists; no args = all routines
  run <name|file>            execute one routine now (launchd entrypoint)
  list                       table of routines and their states
  remove <name> [--purge]    unschedule; --purge also deletes the md

Schedule syntax: daily HH:MM | Mon-Fri HH:MM | weekly Mon[,Tue,...] HH:MM | hourly :MM
Frontmatter: name, schedule, model, variant, agent, dir, auto, notify, timeout, enabled
Model deadlines: model_header_timeout, model_chunk_timeout, model_request_timeout
Body = prompt, passed to `opencode run` via stdin at fire time.
In herdr, the run is followed by `opencode --session <id>` in the same pane,
which reopens that exact session in the TUI for follow-ups; the id comes from
`opencode session list`, matched on the routine's session title and fire time.
A notification fires when
the run finishes (click focuses herdr; terminal-notifier if installed, else
osascript). Headless runs exit when done and also notify.
Logs: ~/.local/state/routine/logs/<slug>.log (or ROUTINES_LOG_DIR)
"""


def main(argv: list[str]) -> int:
    try:
        cmd = argv[0] if argv else "help"
        if cmd == "create" and len(argv) >= 3:
            cmd_create(argv[1], argv[2])
        elif cmd in ("install", "refresh"):
            cmd_install(argv[1:])
        elif cmd == "run" and len(argv) >= 2:
            cmd_run(argv[1])
        elif cmd == "execute" and len(argv) >= 3:
            return cmd_execute(argv[1], argv[2])
        elif cmd == "notify-done" and len(argv) >= 3:
            cmd_notify_done(argv[1], argv[2] == "1")
        elif cmd == "session-id" and len(argv) >= 3:
            cmd_session_id(argv[1], argv[2], argv[3] if len(argv) > 3 else None)
        elif cmd in ("list", "ls"):
            cmd_list()
        elif cmd in ("remove", "rm") and len(argv) >= 2:
            cmd_remove(argv[1], purge="--purge" in argv)
        elif cmd in ("help", "--help", "-h"):
            print(USAGE)
        else:
            print(USAGE, file=sys.stderr)
            return 1
        return 0
    except RoutineError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or "").strip()
        print(f"error: command failed: {shlex.join(e.cmd)}\n{detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
