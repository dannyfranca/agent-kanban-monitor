# agent-kanban-monitor

Generic local monitors for Hermes Agent Kanban boards.

V1 provides `kanban-blocked-watchdog`: a small CLI that polls `hermes kanban list` for blocked tasks, remembers which blocked task IDs were already reported, and prints a compact Telegram-friendly markdown message only when a task is newly blocked.

No new blocked tasks means empty stdout. This is intentional so the tool works cleanly with Hermes cron `no_agent` mode: empty stdout stays silent; non-zero exit creates an alert.

## Features

- Generic: no hardcoded board names, chat IDs, Danny-specific paths, or dashboard hosts.
- Configurable board/status/assignee/tenant filters.
- Configurable state path and dashboard URL template.
- Idempotent notifications while a task remains blocked.
- Self-cleaning state when tasks leave blocked status, with TTL pruning as a safety net.
- Atomic state writes.
- Short stderr errors and non-zero exits for config, CLI, or JSON failures.

## Requirements

- Python 3.10+
- Hermes Agent installed and available as `hermes` on `PATH`
- Hermes Kanban initialized/configured for the profile that runs the script

Hermes docs:
- Kanban: https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban
- Cron: https://hermes-agent.nousresearch.com/docs/user-guide/features/cron
- Profiles: https://hermes-agent.nousresearch.com/docs/user-guide/profiles

## Install locally

From this repo:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

Verify the CLI:

```bash
kanban-blocked-watchdog --help
```

Run tests in a development install (see Development below).

## Configure

Copy the sample config and edit paths/URLs for your profile:

```bash
mkdir -p ~/.config/agent-kanban-monitor
cp examples/config.example.json ~/.config/agent-kanban-monitor/blocked-watchdog.json
$EDITOR ~/.config/agent-kanban-monitor/blocked-watchdog.json
```

Example:

```json
{
  "enabled": true,
  "board": "default",
  "status": "blocked",
  "assignee": null,
  "tenant": null,
  "state_path": "~/.hermes/kanban-blocked-watchdog/state.json",
  "dashboard_url_template": "http://YOUR-HERMES-HOST:9119/kanban?task={task_id}",
  "title_max_chars": 120,
  "include_reason": true,
  "max_state_age_days": 30
}
```

Notes:
- `dashboard_url_template` must include `{task_id}`. It may also include `{board}`.
- For phone/mobile use, set a reachable URL, e.g. Tailnet: `http://agent:9119/kanban?task={task_id}`.
- Do not put polling frequency in this config. Frequency belongs to the scheduler: Hermes cron `every 5m` / `every 10m`, or systemd `OnUnitActiveSec=5min`.

## Run once

```bash
kanban-blocked-watchdog --config ~/.config/agent-kanban-monitor/blocked-watchdog.json
```

Output when a task is newly blocked:

```markdown
🚧 **Kanban needs attention**

⏸ `t_aa4b9231` — Implement WhatsApp cart total in holder-name prompt
🔗 http://agent:9119/kanban?task=t_aa4b9231

Tip: unblock or comment from the Kanban dashboard when ready.
```

Multiple newly blocked tasks are grouped in one message.

If nothing is newly blocked, stdout is empty.

## Recommended: Hermes cron `no_agent` mode

Hermes cron can run scripts on a schedule and deliver stdout directly. Use `no_agent=True` so no LLM is called:

- non-empty stdout is sent as the notification;
- empty stdout is silent;
- non-zero exit alerts you that the watchdog is broken.

Create a wrapper script under the Hermes profile that should own the cron job. Relative cron scripts resolve from that profile's `scripts/` directory: the default profile uses `~/.hermes/scripts`, while named profiles use `~/.hermes/profiles/<profile>/scripts`.

For the default profile, create `~/.hermes/scripts/kanban-blocked-watchdog.sh`:

```bash
mkdir -p ~/.hermes/scripts
$EDITOR ~/.hermes/scripts/kanban-blocked-watchdog.sh
```

```bash
#!/usr/bin/env bash
set -euo pipefail
cd /path/to/agent-kanban-monitor
exec .venv/bin/kanban-blocked-watchdog \
  --config "$HOME/.config/agent-kanban-monitor/blocked-watchdog.json"
```

Make it executable:

```bash
chmod +x ~/.hermes/scripts/kanban-blocked-watchdog.sh
```

Then create a Hermes cron job from a Hermes session or automation with these fields:

```python
cronjob(
    action="create",
    name="Kanban blocked-task watchdog",
    schedule="every 5m",
    script="kanban-blocked-watchdog.sh",
    no_agent=True,
)
```

Use any frequency you want (`every 5m`, `every 10m`, `0 9 * * *`, etc.). The script stays idempotent regardless of frequency.

## Alternative: systemd user timer

If you prefer local systemd scheduling, copy the example units, edit the placeholders, then enable the timer:

```bash
mkdir -p ~/.config/systemd/user
cp examples/kanban-blocked-watchdog.service ~/.config/systemd/user/
cp examples/kanban-blocked-watchdog.timer ~/.config/systemd/user/
$EDITOR ~/.config/systemd/user/kanban-blocked-watchdog.service
$EDITOR ~/.config/systemd/user/kanban-blocked-watchdog.timer
systemctl --user daemon-reload
systemctl --user enable --now kanban-blocked-watchdog.timer
```

Edit both `WorkingDirectory` and `ExecStart` in the service so they point at your repo venv and config path. Edit `OnUnitActiveSec` in the timer for the desired frequency.

Systemd will not deliver Telegram notifications by itself; use Hermes cron if you want direct gateway delivery. The timer is useful for logging or for a custom delivery wrapper.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e . pytest
python -m pytest -q
```

The test suite covers:
- first notification and state recording;
- silence for unchanged blocked tasks;
- state cleanup when tasks leave blocked status;
- re-notification after a task leaves and becomes blocked again;
- grouped messages;
- TTL pruning;
- dashboard URL substitution;
- malformed config / invalid Kanban JSON / failed Kanban command safety.
