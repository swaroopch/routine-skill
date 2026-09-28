---
name: routine
description: Create, list, run, and remove recurring OpenCode tasks on macOS. Each routine is a Markdown file with a schedule and prompt, compiled to a launchd agent. Runs headlessly or in an optional Herdr tab. Use for requests to create or manage routines, scheduled tasks, or daily and weekly recurring prompts.
license: Apache-2.0
---

## Routines

Manage scheduled OpenCode prompts using the bundled `scripts/routines.py`.
Resolve that script relative to this skill's installed directory, not the
current project. Requires macOS, `uv`, and an authenticated `opencode` on PATH.
Herdr and `terminal-notifier` are optional.

## Invocation

```bash
uv run --no-project python <skill-directory>/scripts/routines.py <command>
```

- `create <name> <schedule>` creates a definition and immediately installs it.
- `install [name|file...]` installs or refreshes schedules; no arguments means all definitions.
- `run <name|file>` executes a prompt now.
- `list` shows definitions and whether a plist exists.
- `remove <name> [--purge]` unschedules a routine; `--purge` also deletes its definition.

Definitions live in `~/.config/routine/routines/` by default. `ROUTINES_DIR`
overrides that location. Do not assume a particular notes app, vault, employer,
project directory, model provider, or external-service integration.

## Definition format

```markdown
---
name: Project summary
schedule: "Mon-Fri 09:00"
model:
variant:
agent:
dir:
auto: false
notify: true
timeout: 45m
enabled: true
---

Summarize recent changes in this project. Do not modify files or contact
external services.
```

Set `dir` to the user's chosen project and `model` to an available
`provider/model`, or leave the model blank to use OpenCode's default.
Blank `dir` uses the launcher's working directory for headless runs; scheduled
launchers and newly created Herdr workspaces start in the user's home directory.
Existing Herdr workspaces can retain their own directory, so set `dir` explicitly.
`variant` and `agent` are optional OpenCode settings.

Schedules: `daily HH:MM`, `Mon-Fri HH:MM`, `weekly Mon HH:MM`,
`weekly Mon,Thu HH:MM`, or `hourly :MM`. Times use the Mac's local time zone.

The parser supports flat `key: value` fields, not full YAML. Put comments
outside the frontmatter; inline comments, nested objects, and multiline values
are unsupported.

## Runtime behavior

- Prompt and execution options are read on every run. Reinstall after changing
  the schedule, name, enabled state, tool locations, or storage configuration.
- `enabled: false` skips execution; reinstall also removes the scheduled plist.
- Logs live in `~/.local/state/routine/logs/`; staged prompts are in the sibling
  `prompts/` directory. They can contain private data. No automatic cleanup occurs.
  `ROUTINES_DATA_DIR` changes the state root; `ROUTINES_LOG_DIR` overrides logs.
- launchd captures stdout/stderr for scheduled headless runs. Herdr runs display
  output in their pane. Manual headless runs print to the calling terminal.
- A reachable Herdr gets a tab in a `routines` workspace. After execution the
  launcher tries to reopen that run's OpenCode session by title and start time,
  with the pane's session record as fallback. It never uses global `--continue`.
- `timeout` accepts seconds or `s`, `m`, or `h` suffixes and terminates the
  OpenCode process group when the limit expires.
- Optional `model_header_timeout`, `model_chunk_timeout`, and
  `model_request_timeout` use the same duration syntax and require an explicit
  `provider/model`. They merge provider timeout options into the child process's
  `OPENCODE_CONFIG_CONTENT`. This integration is version-sensitive; see README.
- `notify: false` suppresses completion notifications. Otherwise the launcher
  tries `terminal-notifier`, then `osascript`.
- `auto: true` passes `--auto` to OpenCode. Keep it false unless the user
  explicitly authorizes unattended permission approval and their version supports it.

## Agent workflow

1. Confirm the name, schedule, local time zone, working directory, model, and
   intended task. Show the definition before installing. Ask for confirmation
   for inferred settings or external actions.
2. Prefer writing the reviewed definition, then calling `install <file>`.
   `create` installs a placeholder immediately, so do not use it before approval.
3. Do not copy private notes, credentials, personal paths, or existing prompts
   into a shared repository. Store definitions outside the skill checkout.
4. A manual `run` executes the task immediately. Obtain approval before a test
   that could modify data, call paid services, or contact anyone.
5. Edit prompt text without reinstalling. Reinstall for scheduler changes.
6. Use `remove` to retire a schedule; use `--purge` only when deletion is requested.
   Removal keeps an enabled definition that a later bulk install can reschedule.
7. Finish with `list`. Its state is based on plist presence, not a health check
   of launchd or proof of successful task execution.
