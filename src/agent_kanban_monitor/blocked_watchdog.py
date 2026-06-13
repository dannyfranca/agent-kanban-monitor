from __future__ import annotations

import argparse
import json
import os
import string
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO


Runner = Callable[[list[str], Mapping[str, str] | None], subprocess.CompletedProcess[str]]


class WatchdogError(Exception):
    """Base error for user-facing watchdog failures."""


class ConfigError(WatchdogError):
    """Raised when configuration cannot be loaded or validated."""


class KanbanCommandError(WatchdogError):
    """Raised when the Hermes Kanban command fails or emits invalid output."""


class StateError(WatchdogError):
    """Raised when notification state cannot be read or written safely."""


@dataclass(frozen=True)
class WatchdogConfig:
    enabled: bool = True
    board: str | None = "default"
    status: str = "blocked"
    assignee: str | None = None
    tenant: str | None = None
    state_path: Path = Path("~/.hermes/kanban-blocked-watchdog/state.json")
    dashboard_url_template: str = "http://localhost:9119/kanban?task={task_id}"
    title_max_chars: int = 120
    include_reason: bool = True
    max_state_age_days: int = 30

    @classmethod
    def from_file(cls, path: str | Path) -> "WatchdogConfig":
        config_path = Path(path)
        try:
            raw = config_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read config {config_path}: {exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid config JSON in {config_path}: {exc}") from exc

        if not isinstance(data, dict):
            raise ConfigError(f"config {config_path} must contain a JSON object")

        def optional_str(name: str) -> str | None:
            value = data.get(name, getattr(cls, name))
            if value is None:
                return None
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"config field {name!r} must be a non-empty string or null")
            return value

        enabled = data.get("enabled", cls.enabled)
        if not isinstance(enabled, bool):
            raise ConfigError("config field 'enabled' must be a boolean")

        title_max_chars = data.get("title_max_chars", cls.title_max_chars)
        if not isinstance(title_max_chars, int) or title_max_chars < 1:
            raise ConfigError("config field 'title_max_chars' must be a positive integer")

        max_state_age_days = data.get("max_state_age_days", cls.max_state_age_days)
        if not isinstance(max_state_age_days, int) or max_state_age_days < 1:
            raise ConfigError("config field 'max_state_age_days' must be a positive integer")

        include_reason = data.get("include_reason", cls.include_reason)
        if not isinstance(include_reason, bool):
            raise ConfigError("config field 'include_reason' must be a boolean")

        state_path_raw = data.get("state_path", str(cls.state_path))
        if not isinstance(state_path_raw, str) or not state_path_raw.strip():
            raise ConfigError("config field 'state_path' must be a non-empty string")

        dashboard_url_template = data.get("dashboard_url_template", cls.dashboard_url_template)
        if not isinstance(dashboard_url_template, str) or "{task_id}" not in dashboard_url_template:
            raise ConfigError("config field 'dashboard_url_template' must be a string containing {task_id}")
        validate_dashboard_url_template(dashboard_url_template)

        status = data.get("status", cls.status)
        if not isinstance(status, str) or not status.strip():
            raise ConfigError("config field 'status' must be a non-empty string")

        board = data.get("board", cls.board)
        if not isinstance(board, str) or not board.strip():
            raise ConfigError("config field 'board' must be a non-empty string")

        return cls(
            enabled=enabled,
            board=board,
            status=status,
            assignee=optional_str("assignee"),
            tenant=optional_str("tenant"),
            state_path=Path(os.path.expanduser(state_path_raw)),
            dashboard_url_template=dashboard_url_template,
            title_max_chars=title_max_chars,
            include_reason=include_reason,
            max_state_age_days=max_state_age_days,
        )

    @property
    def board_key(self) -> str:
        return self.board or "default"

    @property
    def state_scope_key(self) -> str:
        parts = [self.board_key]
        if self.status != "blocked":
            parts.append(f"status={self.status}")
        if self.assignee:
            parts.append(f"assignee={self.assignee}")
        if self.tenant:
            parts.append(f"tenant={self.tenant}")
        return "|".join(parts)


def default_runner(args: list[str], env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    subprocess_env = os.environ.copy()
    if env:
        subprocess_env.update(env)
    return subprocess.run(args, check=False, capture_output=True, text=True, env=subprocess_env)


def run_once(config_path: str | Path, *, runner: Runner | None = None, now: int | None = None) -> str:
    config = WatchdogConfig.from_file(config_path)
    if not config.enabled:
        return ""

    current_time = int(time.time() if now is None else now)
    tasks = fetch_blocked_tasks(config, runner=runner or default_runner)
    state = load_state(config.state_path)
    new_state, newly_blocked = update_state(config, state, tasks, now=current_time)
    save_state_atomic(config.state_path, new_state)

    if not newly_blocked:
        return ""
    return render_message(config, newly_blocked)


def fetch_blocked_tasks(config: WatchdogConfig, *, runner: Runner) -> list[dict[str, Any]]:
    args = ["hermes", "kanban", "--board", config.board_key, "list", "--status", config.status, "--json"]
    command_env: dict[str, str] = {}
    if config.board:
        command_env["HERMES_KANBAN_BOARD"] = config.board
        command_env["HERMES_KANBAN_DB"] = ""
    if config.assignee:
        args.extend(["--assignee", config.assignee])
    if config.tenant:
        args.extend(["--tenant", config.tenant])

    try:
        completed = runner(args, command_env or None)
    except OSError as exc:
        raise KanbanCommandError(f"failed to run hermes kanban list: {exc}") from exc

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise KanbanCommandError(f"hermes kanban list failed with exit code {completed.returncode}{detail}")

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise KanbanCommandError(f"invalid Kanban JSON: {exc}") from exc

    return extract_tasks(payload)


def extract_tasks(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        candidates = _first_list_value(payload, ("tasks", "items", "data", "results"))
        if candidates is None and isinstance(payload.get("data"), dict):
            candidates = _first_list_value(payload["data"], ("tasks", "items", "results"))
    else:
        candidates = None

    if not isinstance(candidates, list):
        raise KanbanCommandError("invalid Kanban JSON: expected a list of tasks")

    tasks: list[dict[str, Any]] = []
    for index, task in enumerate(candidates):
        if not isinstance(task, dict):
            raise KanbanCommandError(f"invalid Kanban JSON: task at index {index} is not an object")
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise KanbanCommandError(f"invalid Kanban JSON: task at index {index} is missing string id")
        tasks.append(task)
    return tasks


def _first_list_value(mapping: Mapping[str, Any], keys: Iterable[str]) -> list[Any] | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, list):
            return value
    return None


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "notified": {}}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot load state {path}: {exc}") from exc

    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("notified"), dict):
        raise ConfigError(f"state {path} must be an object with version=1 and notified object")
    return data


def update_state(
    config: WatchdogConfig,
    state: dict[str, Any],
    current_tasks: Sequence[Mapping[str, Any]],
    *,
    now: int,
) -> tuple[dict[str, Any], list[Mapping[str, Any]]]:
    current_scope = config.state_scope_key
    current_by_key = {state_key(current_scope, str(task["id"])): task for task in current_tasks}
    notified = state.get("notified", {})
    ttl_seconds = config.max_state_age_days * 24 * 60 * 60

    next_notified: dict[str, dict[str, Any]] = {}
    newly_blocked: list[Mapping[str, Any]] = []

    for key, existing in notified.items():
        if not isinstance(existing, dict):
            continue
        if entry_scope(key, existing) != current_scope and not entry_is_stale(existing, now, ttl_seconds):
            next_notified[key] = dict(existing)

    for key, task in current_by_key.items():
        existing = notified.get(key)
        if not isinstance(existing, dict):
            newly_blocked.append(task)
            next_notified[key] = new_state_entry(config.board_key, current_scope, str(task["id"]), now, now)
            continue

        if entry_is_stale(existing, now, ttl_seconds):
            newly_blocked.append(task)
            next_notified[key] = new_state_entry(config.board_key, current_scope, str(task["id"]), now, now)
            continue

        entry = dict(existing)
        entry["task_id"] = str(task["id"])
        entry["board"] = config.board_key
        if current_scope != config.board_key:
            entry["scope"] = current_scope
        else:
            entry.pop("scope", None)
        entry["last_seen_blocked_at"] = now
        next_notified[key] = entry

    return {"version": 1, "notified": next_notified}, newly_blocked


def entry_board(key: str, entry: Mapping[str, Any]) -> str:
    board = entry.get("board")
    if isinstance(board, str) and board:
        return board
    return entry_scope(key, entry).split("|", 1)[0]


def entry_scope(key: str, entry: Mapping[str, Any]) -> str:
    scope = entry.get("scope")
    if isinstance(scope, str) and scope:
        return scope
    return key.rsplit(":", 1)[0]


def entry_is_stale(entry: Mapping[str, Any], now: int, ttl_seconds: int) -> bool:
    last_seen = int(entry.get("last_seen_blocked_at") or entry.get("notified_at") or 0)
    return now - last_seen > ttl_seconds


def state_key(scope: str, task_id: str) -> str:
    return f"{scope}:{task_id}"


def new_state_entry(board: str, scope: str, task_id: str, notified_at: int, last_seen_blocked_at: int) -> dict[str, Any]:
    entry = {
        "task_id": task_id,
        "board": board,
        "notified_at": notified_at,
        "last_seen_blocked_at": last_seen_blocked_at,
    }
    if scope != board:
        entry["scope"] = scope
    return entry


def save_state_atomic(path: Path, state: Mapping[str, Any]) -> None:
    tmp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as tmp:
            tmp.write(serialized)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, path)
    except OSError as exc:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise StateError(f"cannot write state {path}: {exc}") from exc


def render_message(config: WatchdogConfig, tasks: Sequence[Mapping[str, Any]]) -> str:
    lines = ["🚧 **Kanban needs attention**", ""]
    for index, task in enumerate(tasks):
        if index:
            lines.append("")
        task_id = str(task["id"])
        title = truncate(str(task.get("title") or "(untitled task)"), config.title_max_chars)
        lines.append(f"⏸ `{task_id}` — {title}")
        if config.include_reason:
            reason = extract_reason(task)
            if reason:
                lines.append(f"↳ {truncate(reason, config.title_max_chars)}")
        lines.append(f"🔗 {format_dashboard_url(config, task_id)}")
    lines.extend(["", "Tip: unblock or comment from the Kanban dashboard when ready."])
    return "\n".join(lines) + "\n"


def truncate(value: str, max_chars: int) -> str:
    clean = " ".join(value.split())
    if len(clean) <= max_chars:
        return clean
    if max_chars == 1:
        return "…"
    return clean[: max_chars - 1] + "…"


def extract_reason(task: Mapping[str, Any]) -> str | None:
    for key in ("blocked_reason", "block_reason", "reason", "result"):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def format_dashboard_url(config: WatchdogConfig, task_id: str) -> str:
    try:
        return config.dashboard_url_template.format(task_id=task_id, board=config.board_key)
    except (KeyError, ValueError) as exc:
        raise ConfigError(f"invalid dashboard_url_template: {exc}") from exc


def validate_dashboard_url_template(template: str) -> None:
    allowed = {"task_id", "board"}
    seen_fields: set[str] = set()
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise ConfigError(f"invalid dashboard_url_template: {exc}") from exc
    for _, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if not field_name:
            raise ConfigError("unsupported positional placeholder {} in dashboard_url_template")
        if field_name not in allowed:
            raise ConfigError(f"unsupported placeholder {{{field_name}}} in dashboard_url_template")
        if format_spec or conversion:
            raise ConfigError(f"unsupported formatting for placeholder {{{field_name}}} in dashboard_url_template")
        seen_fields.add(field_name)
    if "task_id" not in seen_fields:
        raise ConfigError("config field 'dashboard_url_template' must contain an actual {task_id} placeholder")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Emit a notification when Hermes Kanban tasks newly enter blocked status.")
    parser.add_argument("--config", required=True, help="Path to watchdog JSON config")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Runner | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    now: int | None = None,
) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        message = run_once(args.config, runner=runner, now=now)
    except WatchdogError as exc:
        print(f"kanban-blocked-watchdog: {exc}", file=err)
        return 1

    if message:
        print(message, end="", file=out)
    return 0


def cli() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    cli()
