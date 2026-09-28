## Routine skill

Schedule recurring OpenCode prompts on macOS. Each routine is a Markdown file:
frontmatter holds its schedule and options, and the body holds its prompt.
The launcher compiles schedules into launchd agents and reads prompts at run time.

This repository contains only the skill, its launcher, and tests. It includes
no saved routines, personal notes, credentials, or machine-specific configuration.

## Requirements

- macOS for scheduling and notifications.
- `uv` and Python 3.10 or newer.
- OpenCode installed, authenticated, and available on PATH.
- Optional: Herdr for terminal tabs and `terminal-notifier` for notifications.

The launcher uses Python's standard library. It does not install OpenCode,
configure provider credentials, or supply organization-specific tools.
Scheduled jobs do not inherit arbitrary shell environment variables. Configure
OpenCode authentication separately; never put API keys in routine definitions.

## Install as a skill

Copy or symlink this repository into the `routine` directory of your agent's
configured skill location. Keep `SKILL.md`, `scripts/`, and their relative
layout intact. Then invoke `/routine` if your agent supports slash commands,
or ask it to use the routine skill.

The launcher also works without a skill loader. From this repository:

```bash
uv run --no-project python scripts/routines.py --help
uv run --no-project python scripts/routines.py list
```

Keep the installation path stable: generated launchd plists reference it.
After moving the skill or its tools, reinstall your schedules.

## Define and install a routine

Save this synthetic example as
`~/.config/routine/routines/project-summary.md`, creating the directory if needed:

```markdown
---
name: Project summary
schedule: "Mon-Fri 09:00"
model:
dir: ~/path/to/project
auto: false
notify: true
timeout: 45m
enabled: true
---

Summarize recent changes in this project. Do not modify files or contact
external services.
```

Replace the example directory with your project path. Leave `model` blank for
OpenCode's default, or set an available `provider/model`.

```bash
uv run --no-project python scripts/routines.py install project-summary
uv run --no-project python scripts/routines.py list
```

Installing creates a recurring job. To execute it immediately:

```bash
uv run --no-project python scripts/routines.py run project-summary
```

To unschedule it without deleting the definition:

```bash
uv run --no-project python scripts/routines.py remove project-summary
```

A subsequent bulk `install` can reschedule that definition. Set
`enabled: false` and reinstall to keep it disabled, or use `remove --purge`
when you also want to delete it.

See [the skill instructions](SKILL.md) for all fields, schedules, and the
approval workflow. Avoid inline frontmatter comments: this is a small flat-field
parser, not a full YAML parser.

## Storage and privacy

| Setting | Default |
| --- | --- |
| `ROUTINES_DIR` | `~/.config/routine/routines` |
| `ROUTINES_DATA_DIR` | `~/.local/state/routine` |
| `ROUTINES_LOG_DIR` | `<data directory>/logs` |
| `ROUTINES_LAUNCH_DIR` | `~/Library/LaunchAgents` |

Staged prompts and the Herdr allocation lock live under the data directory.
Directory overrides accept `~` and are saved into generated plists. Executable
locations are discovered from PATH when installing; no package-manager location
or private CLI directory is hard-coded.

Runtime data stays outside this repository by default. New definition, state,
and log directories are owner-only. Existing directories and custom parent
directories retain their permissions; review them yourself.
Logs, staged prompts, OpenCode sessions, and notifications may reveal task
content. There is no automatic retention policy. Excluding files from Git does
not erase files already committed.

The launcher forwards the calling process's environment during manual execution,
but generated plists contain only tool paths and storage settings, not arbitrary
environment variables or credentials. Add any task-specific tools to your own
execution environment. Keep `auto: false` unless you intend unattended permission
approval.

## Compatibility and limitations

This is a macOS launchd scheduler, not a Linux or Windows scheduler.
Herdr, OpenCode flags, and provider timeout options depend on the installed
versions. Verify them before relying on unattended execution.

The source skill records runtime timeout testing against OpenCode 1.18.31:
header and chunk stalls retried, while full-request timeouts exited with an
error. Those checks were not repeated for this standalone package. After an
upgrade, verify config merging and fault-test stalled headers, stalled chunks,
and heartbeat-only streams. Unit tests verify launcher wiring, not those
external runtime behaviors.

Routine names should be unique. Avoid overlapping runs of the same routine:
prompt staging and session lookup are not designed to isolate same-name runs.
`list` reports plist presence, not launchd health. Scheduling follows the Mac's
local time zone; this package does not implement its own missed-run replay.

## Tests

```bash
uv run --no-project python -m unittest discover -s tests -v
```

Tests mock external tools and do not install schedules or call model providers.

## License

[Apache License 2.0](LICENSE).
