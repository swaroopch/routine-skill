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

### 1. Check prerequisites

On the Mac that will run the schedules, check that the required tools are
available:

```bash
git --version
uv --version
opencode --version
```

Install any missing tools and configure OpenCode with your chosen provider
before continuing. Confirm that an ordinary OpenCode session works.

### 2. Clone into your skill directory

The commands below use the shared `~/.agents/skills` directory. If your agent
does not discover skills there, substitute its configured skill directory.
Replace the placeholder URL with this repository's clone URL once it is hosted.
For a private repository, authenticate Git with an account that has access first.

```bash
REPO_URL="https://github.com/OWNER/routine-skill.git"
mkdir -p "$HOME/.agents/skills"
git clone "$REPO_URL" "$HOME/.agents/skills/routine"
```

If the destination already exists, inspect it rather than overwriting it.
Keep the complete repository layout, including `SKILL.md` and `scripts/`;
copying only `SKILL.md` will not install the launcher.

### 3. Verify the installation

```bash
uv run --no-project python "$HOME/.agents/skills/routine/scripts/routines.py" --help
uv run --no-project python "$HOME/.agents/skills/routine/scripts/routines.py" list
```

These commands do not schedule jobs or call a model provider. A fresh installation
shows an empty routine list.

Restart your agent or reload its skills, then invoke `/routine` if it supports
slash commands. Otherwise, ask it to use the `routine` skill. If the skill is not
discovered, check the agent's configured skill directory. The launcher also
works directly from the command line without a skill loader.

### 4. Create your first routine

Follow the example below, or ask your agent:

> Use the routine skill to prepare a weekday project summary at 09:00. Ask me
> for the project directory and model, and show the definition before scheduling it.

Installing the skill alone does not install any schedules. For the relative
commands in the rest of this README, first enter the checkout:

```bash
cd "$HOME/.agents/skills/routine"
```

Keep this installation path stable: generated launchd plists reference it.
After moving the skill or its tools, reinstall your schedules with
`uv run --no-project python scripts/routines.py install`.

To update a Git-based installation:

```bash
git -C "$HOME/.agents/skills/routine" pull --ff-only
```

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
