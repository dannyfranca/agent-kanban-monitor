import io
import json
import subprocess
from pathlib import Path

import pytest

from agent_kanban_monitor.blocked_watchdog import main, run_once


def write_config(tmp_path: Path, **overrides) -> Path:
    config = {
        "enabled": True,
        "board": "default",
        "status": "blocked",
        "assignee": None,
        "tenant": None,
        "state_path": str(tmp_path / "state.json"),
        "dashboard_url_template": "http://agent:9119/kanban?task={task_id}",
        "title_max_chars": 120,
        "include_reason": True,
        "max_state_age_days": 30,
    }
    config.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


class FakeRunner:
    def __init__(self, tasks):
        self.tasks = tasks
        self.last_args = None
        self.last_env = {}

    def __call__(self, args, env=None):
        self.last_args = args
        self.last_env = env or {}
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=json.dumps(self.tasks), stderr="")


def fake_runner(tasks):
    return FakeRunner(tasks)


def read_state(config_path: Path) -> dict:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    return json.loads(Path(config["state_path"]).read_text(encoding="utf-8"))


def test_first_blocked_task_emits_message_and_records_state(tmp_path):
    config_path = write_config(tmp_path)
    runner = fake_runner([
        {"id": "t_aa4b9231", "title": "Implement WhatsApp cart total in holder-name prompt", "result": "needs product decision"}
    ])

    message = run_once(config_path, runner=runner, now=1_781_332_000)

    assert "🚧 **Kanban needs attention**" in message
    assert "⏸ `t_aa4b9231` — Implement WhatsApp cart total in holder-name prompt" in message
    assert "needs product decision" in message
    assert "🔗 http://agent:9119/kanban?task=t_aa4b9231" in message
    assert runner.last_args == ["hermes", "kanban", "--board", "default", "list", "--status", "blocked", "--json"]
    assert runner.last_env["HERMES_KANBAN_BOARD"] == "default"
    assert read_state(config_path) == {
        "version": 1,
        "notified": {
            "default:t_aa4b9231": {
                "task_id": "t_aa4b9231",
                "board": "default",
                "notified_at": 1_781_332_000,
                "last_seen_blocked_at": 1_781_332_000,
            }
        },
    }


def test_unchanged_blocked_task_emits_nothing_on_subsequent_run(tmp_path):
    config_path = write_config(tmp_path)
    runner = fake_runner([{"id": "t_same", "title": "Still blocked"}])

    first = run_once(config_path, runner=runner, now=10)
    second = run_once(config_path, runner=runner, now=20)

    assert "t_same" in first
    assert second == ""
    state = read_state(config_path)
    assert state["notified"]["default:t_same"]["notified_at"] == 10
    assert state["notified"]["default:t_same"]["last_seen_blocked_at"] == 20


def test_main_emits_no_stdout_when_there_are_no_new_blocked_tasks(tmp_path):
    config_path = write_config(tmp_path)
    run_once(config_path, runner=fake_runner([{"id": "t_same", "title": "Still blocked"}]), now=10)
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["--config", str(config_path)],
        runner=fake_runner([{"id": "t_same", "title": "Still blocked"}]),
        stdout=stdout,
        stderr=stderr,
        now=20,
    )

    assert exit_code == 0
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == ""


def test_main_emits_new_blocked_task_markdown_on_stdout(tmp_path):
    config_path = write_config(tmp_path)
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["--config", str(config_path)],
        runner=fake_runner([{"id": "t_cli", "title": "CLI notification"}]),
        stdout=stdout,
        stderr=stderr,
        now=10,
    )

    assert exit_code == 0
    assert "🚧 **Kanban needs attention**" in stdout.getvalue()
    assert "⏸ `t_cli` — CLI notification" in stdout.getvalue()
    assert stderr.getvalue() == ""


def test_task_removed_from_blocked_list_is_removed_from_state(tmp_path):
    config_path = write_config(tmp_path)

    run_once(config_path, runner=fake_runner([{"id": "t_done", "title": "Unblock me"}]), now=10)
    message = run_once(config_path, runner=fake_runner([]), now=20)

    assert message == ""
    assert read_state(config_path)["notified"] == {}


def test_same_task_blocked_again_after_removal_emits_again(tmp_path):
    config_path = write_config(tmp_path)
    task = {"id": "t_repeat", "title": "Needs attention again"}

    first = run_once(config_path, runner=fake_runner([task]), now=10)
    run_once(config_path, runner=fake_runner([]), now=20)
    second = run_once(config_path, runner=fake_runner([task]), now=30)

    assert first.count("t_repeat") >= 1
    assert second.count("t_repeat") >= 1
    assert read_state(config_path)["notified"]["default:t_repeat"]["notified_at"] == 30


def test_multiple_blocked_tasks_are_grouped_in_one_message(tmp_path):
    config_path = write_config(tmp_path)

    message = run_once(
        config_path,
        runner=fake_runner([
            {"id": "t_one", "title": "First blocked task"},
            {"id": "t_two", "title": "Second blocked task"},
        ]),
        now=10,
    )

    assert message.startswith("🚧 **Kanban needs attention**")
    assert message.count("⏸ `") == 2
    assert "t_one" in message
    assert "t_two" in message
    assert "Tip: unblock or comment from the Kanban dashboard when ready." in message


def test_mixed_old_and_new_blocked_tasks_emit_only_new_task(tmp_path):
    config_path = write_config(tmp_path)

    run_once(config_path, runner=fake_runner([{"id": "t_old", "title": "Already blocked"}]), now=10)
    message = run_once(
        config_path,
        runner=fake_runner([
            {"id": "t_old", "title": "Already blocked"},
            {"id": "t_new", "title": "Newly blocked"},
        ]),
        now=20,
    )

    assert "t_new" in message
    assert "Newly blocked" in message
    assert "t_old" not in message
    assert "Already blocked" not in message


def test_state_ttl_prunes_stale_entries(tmp_path):
    config_path = write_config(tmp_path, max_state_age_days=1)
    state_path = Path(json.loads(config_path.read_text(encoding="utf-8"))["state_path"])
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "notified": {
                    "default:t_stale": {
                        "task_id": "t_stale",
                        "board": "default",
                        "notified_at": 0,
                        "last_seen_blocked_at": 0,
                    },
                    "default:t_fresh": {
                        "task_id": "t_fresh",
                        "board": "default",
                        "notified_at": 99_000,
                        "last_seen_blocked_at": 99_000,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    run_once(config_path, runner=fake_runner([{"id": "t_fresh", "title": "Fresh"}]), now=100_000)

    assert set(read_state(config_path)["notified"]) == {"default:t_fresh"}


def test_state_ttl_prunes_stale_entries_from_other_boards(tmp_path):
    config_path = write_config(tmp_path, board="ops", max_state_age_days=1)
    state_path = Path(json.loads(config_path.read_text(encoding="utf-8"))["state_path"])
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "notified": {
                    "default:t_stale": {
                        "task_id": "t_stale",
                        "board": "default",
                        "notified_at": 0,
                        "last_seen_blocked_at": 0,
                    },
                    "default:t_fresh": {
                        "task_id": "t_fresh",
                        "board": "default",
                        "notified_at": 99_000,
                        "last_seen_blocked_at": 99_000,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    run_once(config_path, runner=fake_runner([]), now=100_000)

    assert set(read_state(config_path)["notified"]) == {"default:t_fresh"}


def test_still_blocked_stale_task_notifies_again_and_refreshes_state(tmp_path):
    config_path = write_config(tmp_path, max_state_age_days=1)
    state_path = Path(json.loads(config_path.read_text(encoding="utf-8"))["state_path"])
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "notified": {
                    "default:t_stale": {
                        "task_id": "t_stale",
                        "board": "default",
                        "notified_at": 0,
                        "last_seen_blocked_at": 0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    message = run_once(config_path, runner=fake_runner([{"id": "t_stale", "title": "Still blocked"}]), now=100_000)

    assert "t_stale" in message
    assert read_state(config_path)["notified"]["default:t_stale"]["notified_at"] == 100_000


def test_shared_state_preserves_other_board_entries(tmp_path):
    config_path = write_config(tmp_path, board="ops")
    state_path = Path(json.loads(config_path.read_text(encoding="utf-8"))["state_path"])
    original_other_board = {
        "task_id": "t_default",
        "board": "default",
        "notified_at": 1,
        "last_seen_blocked_at": 1,
    }
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "notified": {
                    "default:t_default": original_other_board,
                    "ops:t_old": {
                        "task_id": "t_old",
                        "board": "ops",
                        "notified_at": 1,
                        "last_seen_blocked_at": 1,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    run_once(config_path, runner=fake_runner([{"id": "t_new", "title": "New ops task"}]), now=10)

    state = read_state(config_path)["notified"]
    assert state["default:t_default"] == original_other_board
    assert "ops:t_old" not in state
    assert "ops:t_new" in state


def test_dashboard_url_template_substitution_and_title_truncation(tmp_path):
    config_path = write_config(
        tmp_path,
        board="ops",
        dashboard_url_template="https://example.test/{board}/tasks/{task_id}",
        title_max_chars=12,
    )

    message = run_once(
        config_path,
        runner=fake_runner([{"id": "t_link", "title": "A title that is intentionally long"}]),
        now=10,
    )

    assert "A title tha…" in message
    assert "https://example.test/ops/tasks/t_link" in message


def test_malformed_dashboard_template_exits_nonzero_without_updating_state(tmp_path):
    config_path = write_config(tmp_path, dashboard_url_template="https://example.test/{task_id}/{tenant}")
    state_path = Path(json.loads(config_path.read_text(encoding="utf-8"))["state_path"])
    original = {"version": 1, "notified": {}}
    state_path.write_text(json.dumps(original), encoding="utf-8")
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["--config", str(config_path)],
        runner=fake_runner([{"id": "t_bad_template", "title": "Bad template"}]),
        stdout=stdout,
        stderr=stderr,
        now=10,
    )

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert "unsupported placeholder" in stderr.getvalue()
    assert json.loads(state_path.read_text(encoding="utf-8")) == original


@pytest.mark.parametrize(
    "template",
    [
        "https://example.test/{task_id}/{board.missing}",
        "https://example.test/{task_id}/{task_id:bad}",
        "https://example.test/{task_id}/{}",
        "https://example.test/{{task_id}}",
    ],
)
def test_dashboard_template_format_errors_exit_nonzero_without_updating_state(tmp_path, template):
    config_path = write_config(tmp_path, dashboard_url_template=template)
    state_path = Path(json.loads(config_path.read_text(encoding="utf-8"))["state_path"])
    original = {"version": 1, "notified": {}}
    state_path.write_text(json.dumps(original), encoding="utf-8")
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["--config", str(config_path)],
        runner=fake_runner([{"id": "t_bad_template", "title": "Bad template"}]),
        stdout=stdout,
        stderr=stderr,
        now=10,
    )

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert json.loads(state_path.read_text(encoding="utf-8")) == original


def test_state_write_failure_exits_nonzero_with_compact_error(tmp_path):
    state_parent = tmp_path / "not-a-directory"
    state_parent.write_text("file blocks directory creation", encoding="utf-8")
    config_path = write_config(tmp_path, state_path=str(state_parent / "state.json"))
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["--config", str(config_path)],
        runner=fake_runner([{"id": "t_write", "title": "Cannot write state"}]),
        stdout=stdout,
        stderr=stderr,
        now=10,
    )

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert "cannot write state" in stderr.getvalue()
    assert "Traceback" not in stderr.getvalue()


def test_optional_filters_are_passed_to_kanban_list(tmp_path):
    config_path = write_config(tmp_path, board="ops", status="review", assignee="coder", tenant="customer-a")
    runner = fake_runner([])

    run_once(config_path, runner=runner, now=10)

    assert runner.last_args == [
        "hermes",
        "kanban",
        "--board",
        "ops",
        "list",
        "--status",
        "review",
        "--json",
        "--assignee",
        "coder",
        "--tenant",
        "customer-a",
    ]
    assert runner.last_env["HERMES_KANBAN_BOARD"] == "ops"
    assert runner.last_env["HERMES_KANBAN_DB"] == ""


def test_disabled_config_exits_silently_without_running_kanban(tmp_path):
    config_path = write_config(tmp_path, enabled=False)
    called = False

    def runner(args):
        nonlocal called
        called = True
        raise AssertionError("runner should not be called")

    assert run_once(config_path, runner=runner, now=10) == ""
    assert called is False


def test_malformed_config_exits_nonzero_and_does_not_write_state(tmp_path):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text('{"enabled": true, "state_path": ', encoding="utf-8")
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(["--config", str(config_path)], stdout=stdout, stderr=stderr, now=10)

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert "invalid config JSON" in stderr.getvalue()
    assert not state_path.exists()


def test_null_board_config_exits_nonzero_without_running_kanban(tmp_path):
    config_path = write_config(tmp_path, board=None)
    stdout = io.StringIO()
    stderr = io.StringIO()
    called = False

    def runner(args, env=None):
        nonlocal called
        called = True
        raise AssertionError("runner should not be called")

    exit_code = main(["--config", str(config_path)], runner=runner, stdout=stdout, stderr=stderr, now=10)

    assert exit_code == 1
    assert called is False
    assert stdout.getvalue() == ""
    assert "board" in stderr.getvalue()


@pytest.mark.parametrize("kanban_stdout", ["not-json", "{}", '[{"title": "missing id"}]'])
def test_invalid_kanban_json_exits_nonzero_without_corrupting_existing_state(tmp_path, kanban_stdout):
    config_path = write_config(tmp_path)
    state_path = Path(json.loads(config_path.read_text(encoding="utf-8"))["state_path"])
    original = {"version": 1, "notified": {"default:t_keep": {"task_id": "t_keep", "board": "default", "notified_at": 1, "last_seen_blocked_at": 1}}}
    state_path.write_text(json.dumps(original), encoding="utf-8")

    def runner(args, env=None):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=kanban_stdout, stderr="")

    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = main(["--config", str(config_path)], runner=runner, stdout=stdout, stderr=stderr, now=10)

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert "invalid Kanban JSON" in stderr.getvalue()
    assert json.loads(state_path.read_text(encoding="utf-8")) == original


def test_failed_kanban_command_exits_nonzero_without_updating_state(tmp_path):
    config_path = write_config(tmp_path)
    state_path = Path(json.loads(config_path.read_text(encoding="utf-8"))["state_path"])
    original = {"version": 1, "notified": {}}
    state_path.write_text(json.dumps(original), encoding="utf-8")

    def runner(args, env=None):
        return subprocess.CompletedProcess(args=args, returncode=2, stdout="", stderr="board unavailable")

    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = main(["--config", str(config_path)], runner=runner, stdout=stdout, stderr=stderr, now=10)

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert "hermes kanban list failed" in stderr.getvalue()
    assert json.loads(state_path.read_text(encoding="utf-8")) == original
