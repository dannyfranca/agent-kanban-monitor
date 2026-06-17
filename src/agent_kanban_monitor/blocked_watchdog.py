from __future__ import annotations

import argparse
import heapq
import json
import os
import re
import string
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO


Runner = Callable[[list[str], Mapping[str, str] | None], subprocess.CompletedProcess[str]]

GITHUB_PR_URL_RE = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)")
GITHUB_PR_SHORTHAND_RE = re.compile(r"github:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)")


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
    include_pr_link: bool = True
    pr_link_label: str = "Open GitHub PR"
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

        include_pr_link = data.get("include_pr_link", cls.include_pr_link)
        if not isinstance(include_pr_link, bool):
            raise ConfigError("config field 'include_pr_link' must be a boolean")

        pr_link_label = data.get("pr_link_label", cls.pr_link_label)
        if not isinstance(pr_link_label, str) or not pr_link_label.strip():
            raise ConfigError("config field 'pr_link_label' must be a non-empty string")

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
            include_pr_link=include_pr_link,
            pr_link_label=pr_link_label.strip(),
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
    command_runner = runner or default_runner
    tasks = fetch_blocked_tasks(config, runner=command_runner)
    state = load_state(config.state_path)
    new_state, newly_blocked = update_state(config, state, tasks, now=current_time)

    if not newly_blocked:
        save_state_atomic(config.state_path, new_state)
        return ""
    newly_blocked = enrich_newly_blocked_tasks(config, newly_blocked, runner=command_runner)
    message = render_message(config, newly_blocked)
    save_state_atomic(config.state_path, new_state)
    return message


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


def enrich_newly_blocked_tasks(
    config: WatchdogConfig,
    tasks: Sequence[Mapping[str, Any]],
    *,
    runner: Runner,
) -> list[Mapping[str, Any]]:
    if not config.include_reason and not config.include_pr_link:
        return list(tasks)
    enriched: list[Mapping[str, Any]] = []
    for task in tasks:
        if not task_needs_detail_enrichment(config, task):
            enriched.append(task)
        else:
            enriched.append(fetch_task_detail(config, str(task["id"]), runner=runner, fallback=task))
    return enriched


def task_needs_detail_enrichment(config: WatchdogConfig, task: Mapping[str, Any]) -> bool:
    needs_reason = config.include_reason and not extract_reason(task)
    needs_pr_link = config.include_pr_link
    return needs_reason or needs_pr_link


def fetch_task_detail(
    config: WatchdogConfig,
    task_id: str,
    *,
    runner: Runner,
    fallback: Mapping[str, Any],
) -> Mapping[str, Any]:
    args = ["hermes", "kanban", "--board", config.board_key, "show", task_id, "--json"]
    command_env: dict[str, str] = {}
    if config.board:
        command_env["HERMES_KANBAN_BOARD"] = config.board
        command_env["HERMES_KANBAN_DB"] = ""

    try:
        completed = runner(args, command_env or None)
    except OSError:
        return fallback

    if completed.returncode != 0:
        return fallback

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return fallback

    if not isinstance(payload, dict):
        return fallback

    enriched = dict(fallback)
    payload_task = payload.get("task")
    if isinstance(payload_task, dict):
        enriched.update(payload_task)
    for key in ("latest_summary",):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            enriched[key] = value.strip()
    enriched["_kanban_detail"] = payload
    return enriched


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
        lines.append(format_dashboard_link(config, task_id))
        pr_url = extract_pr_url(task) if config.include_pr_link else None
        if pr_url:
            lines.append(format_pr_link(config, pr_url))
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
    for key in ("blocked_reason", "block_reason", "reason", "result", "latest_summary"):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def extract_pr_url(task: Mapping[str, Any]) -> str | None:
    for value in pr_search_values(task):
        if not isinstance(value, str):
            continue
        match = GITHUB_PR_URL_RE.search(value)
        if match:
            return normalize_github_pr_url(*match.groups())
        match = GITHUB_PR_SHORTHAND_RE.search(value)
        if match:
            return normalize_github_pr_url(*match.groups())
    return None


def pr_search_values(task: Mapping[str, Any]) -> Iterable[Any]:
    detail = task.get("_kanban_detail")
    if isinstance(detail, Mapping):
        contexts: list[tuple[tuple[int, int], Iterable[Any]]] = []
        comments = detail.get("comments")
        if isinstance(comments, list):
            for index, comment in newest_mappings(comments):
                contexts.append((sort_timestamp(comment, index), [comment.get("body")]))

        latest_summary = detail.get("latest_summary")
        latest_summary_run_key: tuple[int, int] | None = None
        runs = detail.get("runs")
        if isinstance(runs, list):
            for index, run in newest_mappings(runs):
                run_key = sort_timestamp(run, index)
                if isinstance(latest_summary, str) and run_contains_text(run, latest_summary):
                    latest_summary_run_key = max(latest_summary_run_key or run_key, run_key)
                contexts.append((run_key, walk_run_pr_values(run)))

        if isinstance(latest_summary, str) and latest_summary.strip():
            contexts.append((latest_summary_run_key or (0, 0), [latest_summary]))

        for _, values in sorted(contexts, key=lambda item: item[0], reverse=True):
            yield from values

        for key in ("summary", "result"):
            yield detail.get(key)

    for key in ("blocked_reason", "block_reason", "latest_summary", "reason", "result", "body", "title"):
        yield task.get(key)


def newest_mappings(values: Sequence[Any], *, limit: int = 50) -> list[tuple[int, Mapping[str, Any]]]:
    candidates = [(index, value) for index, value in enumerate(values) if isinstance(value, Mapping)]
    return heapq.nlargest(limit, candidates, key=lambda item: sort_timestamp(item[1], item[0]))


def sort_timestamp(value: Mapping[str, Any], index: int) -> tuple[int, int]:
    primary = value.get("ended_at") or value.get("created_at") or value.get("started_at") or 0
    secondary = value.get("id") or index
    return (parse_timestamp(primary), parse_timestamp(secondary))


def run_contains_text(run: Mapping[str, Any], text: str) -> bool:
    return any(run.get(key) == text for key in ("summary", "result", "body"))


def parse_timestamp(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
        try:
            parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
        except ValueError:
            return 0
    return 0


def walk_run_pr_values(run: Mapping[str, Any]) -> Iterable[str]:
    for key in ("summary", "result", "body"):
        if key in run:
            yield from walk_pr_values(run[key])
    if "metadata" in run:
        yield from walk_metadata_pr_values(run["metadata"])
    if "error" in run:
        yield from walk_pr_values(run["error"])


def walk_metadata_pr_values(value: Any, *, depth: int = 0) -> Iterable[str]:
    if depth > 6:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        priority_keys = (
            "pr_url",
            "pr_link",
            "github_pr_url",
            "github_pr",
            "pull_request_url",
            "pull_request",
            "pr",
            "handoff",
            "review",
            "review_required",
        )
        seen: set[str] = set()
        for key in priority_keys:
            if key in value:
                seen.add(key)
                yield from walk_metadata_pr_values(value[key], depth=depth + 1)
        for index, (key, nested) in enumerate(value.items()):
            if index >= 50:
                break
            if key not in seen:
                yield from walk_metadata_pr_values(nested, depth=depth + 1)
    elif isinstance(value, list):
        for item in value[:20]:
            yield from walk_metadata_pr_values(item, depth=depth + 1)


def walk_pr_values(value: Any, *, depth: int = 0) -> Iterable[str]:
    if depth > 6:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for index, nested in enumerate(value.values()):
            if index >= 50:
                break
            yield from walk_pr_values(nested, depth=depth + 1)
    elif isinstance(value, list):
        for item in value[:20]:
            yield from walk_pr_values(item, depth=depth + 1)


def normalize_github_pr_url(owner: str, repo: str, number: str) -> str:
    return f"https://github.com/{owner}/{repo}/pull/{number}"


def format_dashboard_url(config: WatchdogConfig, task_id: str) -> str:
    try:
        return config.dashboard_url_template.format(task_id=task_id, board=config.board_key)
    except (KeyError, ValueError) as exc:
        raise ConfigError(f"invalid dashboard_url_template: {exc}") from exc


def format_dashboard_link(config: WatchdogConfig, task_id: str) -> str:
    target = format_dashboard_url(config, task_id)
    if target.startswith("[") and "](" in target and target.endswith(")"):
        return target
    return f"[Open Kanban task]({target})"


def format_pr_link(config: WatchdogConfig, url: str) -> str:
    return f"[{config.pr_link_label}]({url})"


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
