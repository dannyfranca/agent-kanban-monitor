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
        "include_pr_link": True,
        "pr_link_label": "Open GitHub PR",
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


def completed_json(args, payload):
    return subprocess.CompletedProcess(args=args, returncode=0, stdout=json.dumps(payload), stderr="")


def read_state(config_path: Path) -> dict:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    return json.loads(Path(config["state_path"]).read_text(encoding="utf-8"))


def test_first_blocked_task_emits_message_and_records_state(tmp_path):
    config_path = write_config(tmp_path)
    runner = fake_runner([
        {"id": "t_aa4b9231", "title": "Implement WhatsApp cart total in holder-name prompt", "result": "needs product decision"}
    ])

    message = run_once(config_path, runner=runner, now=1_781_332_000)

    assert "🟢 **Kanban ready — needs product decision**" in message
    assert "`t_aa4b9231`" in message
    assert "[Open Kanban task](http://agent:9119/kanban?task=t_aa4b9231)" in message
    assert "🚧 **Kanban needs attention**" not in message
    assert "Tip: unblock or comment" not in message
    assert runner.last_args == ["hermes", "kanban", "--board", "default", "show", "t_aa4b9231", "--json"]
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


def test_new_blocked_task_uses_latest_summary_from_show_when_list_has_no_reason(tmp_path):
    config_path = write_config(tmp_path)
    calls = []

    def runner(args, env=None):
        calls.append(args)
        if "list" in args:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps([{"id": "t_reason", "title": "Verbose task title"}]),
                stderr="",
            )
        if "show" in args:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps(
                    {
                        "task": {"id": "t_reason", "title": "Verbose task title"},
                        "latest_summary": "permission-required: grant workflow scope",
                    }
                ),
                stderr="",
            )
        raise AssertionError(args)

    message = run_once(config_path, runner=runner, now=10)

    assert "🟢 **Kanban ready — permission-required: grant workflow scope**" in message
    assert calls[0] == ["hermes", "kanban", "--board", "default", "list", "--status", "blocked", "--json"]
    assert calls[1] == ["hermes", "kanban", "--board", "default", "show", "t_reason", "--json"]


def test_raw_github_pr_url_from_handoff_comment_renders_pr_link(tmp_path):
    config_path = write_config(tmp_path)

    def runner(args, env=None):
        if "list" in args:
            return completed_json(args, [{"id": "t_review", "title": "Review required", "result": "review-required: PR opened"}])
        if "show" in args:
            return completed_json(
                args,
                {
                    "task": {"id": "t_review", "title": "Review required", "result": "review-required: PR opened"},
                    "comments": [
                        {
                            "author": "coder",
                            "created_at": 1_781_000_000,
                            "body": "handoff:\nPR: https://github.com/dannyfranca/agent-alert-monitor/pull/7\nTests pass",
                        }
                    ],
                },
            )
        raise AssertionError(args)

    message = run_once(config_path, runner=runner, now=10)

    assert "[Open Kanban task](http://agent:9119/kanban?task=t_review)\n[Open GitHub PR](https://github.com/dannyfranca/agent-alert-monitor/pull/7)" in message


def test_github_pr_shorthand_from_handoff_comment_is_normalized(tmp_path):
    config_path = write_config(tmp_path)

    def runner(args, env=None):
        if "list" in args:
            return completed_json(args, [{"id": "t_safe", "title": "Safe PR ref"}])
        if "show" in args:
            return completed_json(
                args,
                {
                    "task": {"id": "t_safe", "title": "Safe PR ref"},
                    "comments": [
                        {
                            "author": "coder",
                            "created_at": 1_781_000_001,
                            "body": "review-required handoff: github:dannyfranca/agent-kanban-monitor/pull/12",
                        }
                    ],
                },
            )
        raise AssertionError(args)

    message = run_once(config_path, runner=runner, now=10)

    assert "[Open GitHub PR](https://github.com/dannyfranca/agent-kanban-monitor/pull/12)" in message
    assert "github:dannyfranca/agent-kanban-monitor/pull/12" not in message


def test_no_pr_reference_leaves_output_without_pr_link(tmp_path):
    config_path = write_config(tmp_path)

    def runner(args, env=None):
        if "list" in args:
            return completed_json(args, [{"id": "t_none", "title": "No PR", "result": "needs product decision"}])
        if "show" in args:
            return completed_json(args, {"task": {"id": "t_none", "title": "No PR", "result": "needs product decision"}, "comments": []})
        raise AssertionError(args)

    message = run_once(config_path, runner=runner, now=10)

    assert "[Open Kanban task](http://agent:9119/kanban?task=t_none)" in message
    assert "Open GitHub PR" not in message


def test_pr_links_can_be_disabled_in_config(tmp_path):
    config_path = write_config(tmp_path, include_pr_link=False)
    calls = []

    def runner(args, env=None):
        calls.append(args)
        if "list" in args:
            return completed_json(
                args,
                [
                    {
                        "id": "t_disabled",
                        "title": "Disabled PR link",
                        "result": "review-required: github:dannyfranca/agent-kanban-monitor/pull/13",
                    }
                ],
            )
        raise AssertionError(args)

    message = run_once(config_path, runner=runner, now=10)

    assert "Open GitHub PR" not in message
    assert len(calls) == 1


def test_multiple_newly_blocked_cards_render_pr_links_independently(tmp_path):
    config_path = write_config(tmp_path)

    def runner(args, env=None):
        if "list" in args:
            return completed_json(
                args,
                [
                    {"id": "t_with_pr", "title": "Card with PR"},
                    {"id": "t_without_pr", "title": "Card without PR"},
                ],
            )
        if "show" in args and "t_with_pr" in args:
            return completed_json(
                args,
                {
                    "task": {"id": "t_with_pr", "title": "Card with PR"},
                    "comments": [{"created_at": 2, "body": "github:owner/repo/pull/99"}],
                },
            )
        if "show" in args and "t_without_pr" in args:
            return completed_json(args, {"task": {"id": "t_without_pr", "title": "Card without PR"}, "comments": []})
        raise AssertionError(args)

    message = run_once(config_path, runner=runner, now=10)

    blocks = [block for block in message.split("\n\n") if "🟢 **Kanban ready" in block]
    with_pr_block, without_pr_block = blocks
    assert "[Open GitHub PR](https://github.com/owner/repo/pull/99)" in with_pr_block
    assert "Open GitHub PR" not in without_pr_block


def test_detail_handoff_pr_takes_precedence_over_stale_list_pr(tmp_path):
    config_path = write_config(tmp_path)

    def runner(args, env=None):
        if "list" in args:
            return completed_json(
                args,
                [
                    {
                        "id": "t_stale_pr",
                        "title": "Stale PR",
                        "result": "old handoff https://github.com/owner/repo/pull/1",
                    }
                ],
            )
        if "show" in args:
            return completed_json(
                args,
                {
                    "task": {"id": "t_stale_pr", "title": "Stale PR"},
                    "comments": [{"created_at": 2, "body": "current handoff github:owner/repo/pull/2"}],
                },
            )
        raise AssertionError(args)

    message = run_once(config_path, runner=runner, now=10)

    assert "[Open GitHub PR](https://github.com/owner/repo/pull/2)" in message
    assert "[Open GitHub PR](https://github.com/owner/repo/pull/1)" not in message


def test_newest_detail_context_wins_across_string_timestamp_comments_and_runs(tmp_path):
    config_path = write_config(tmp_path)

    def runner(args, env=None):
        if "list" in args:
            return completed_json(args, [{"id": "t_newest", "title": "Newest context"}])
        if "show" in args:
            return completed_json(
                args,
                {
                    "task": {"id": "t_newest", "title": "Newest context"},
                    "comments": [{"created_at": "2026-06-16T10:00:00Z", "body": "older github:owner/repo/pull/10"}],
                    "runs": [
                        {
                            "id": 12,
                            "created_at": "2026-06-16T09:00:00Z",
                            "ended_at": "2026-06-16T11:00:00Z",
                            "metadata": {"handoff": {"pr_url": "https://github.com/owner/repo/pull/11"}},
                        }
                    ],
                },
            )
        raise AssertionError(args)

    message = run_once(config_path, runner=runner, now=10)

    assert "[Open GitHub PR](https://github.com/owner/repo/pull/11)" in message
    assert "pull/10" not in message


def test_latest_summary_pr_takes_precedence_over_older_detail_context(tmp_path):
    config_path = write_config(tmp_path)

    def runner(args, env=None):
        if "list" in args:
            return completed_json(args, [{"id": "t_latest_summary", "title": "Latest summary"}])
        if "show" in args:
            return completed_json(
                args,
                {
                    "task": {"id": "t_latest_summary", "title": "Latest summary"},
                    "comments": [{"created_at": "2026-06-16T10:00:00Z", "body": "older github:owner/repo/pull/20"}],
                    "runs": [{"ended_at": "2026-06-16T11:00:00Z", "summary": "current review handoff github:owner/repo/pull/21"}],
                    "latest_summary": "current review handoff github:owner/repo/pull/21",
                },
            )
        raise AssertionError(args)

    message = run_once(config_path, runner=runner, now=10)

    assert "[Open GitHub PR](https://github.com/owner/repo/pull/21)" in message
    assert "[Open GitHub PR](https://github.com/owner/repo/pull/20)" not in message


def test_latest_summary_does_not_replace_existing_block_reason(tmp_path):
    config_path = write_config(tmp_path)

    def runner(args, env=None):
        if "list" in args:
            return completed_json(args, [{"id": "t_reason_priority", "title": "Reason priority", "result": "needs product decision"}])
        if "show" in args:
            return completed_json(
                args,
                {
                    "task": {"id": "t_reason_priority", "title": "Reason priority", "result": "needs product decision"},
                    "runs": [{"ended_at": "2026-06-16T11:00:00Z", "summary": "worker summary github:owner/repo/pull/32"}],
                    "latest_summary": "worker summary github:owner/repo/pull/32",
                },
            )
        raise AssertionError(args)

    message = run_once(config_path, runner=runner, now=10)

    assert "🟢 **Kanban ready — needs product decision**" in message
    assert "🟢 **Kanban ready — worker summary" not in message
    assert "[Open GitHub PR](https://github.com/owner/repo/pull/32)" in message


def test_newer_context_wins_over_unmatched_latest_summary(tmp_path):
    config_path = write_config(tmp_path)

    def runner(args, env=None):
        if "list" in args:
            return completed_json(args, [{"id": "t_corrected", "title": "Corrected PR"}])
        if "show" in args:
            return completed_json(
                args,
                {
                    "task": {"id": "t_corrected", "title": "Corrected PR"},
                    "runs": [{"ended_at": "2026-06-16T09:00:00Z", "summary": "old github:owner/repo/pull/40"}],
                    "comments": [{"created_at": "2026-06-16T12:00:00", "body": "corrected github:owner/repo/pull/41"}],
                    "latest_summary": "old github:owner/repo/pull/40",
                },
            )
        raise AssertionError(args)

    message = run_once(config_path, runner=runner, now=10)

    assert "[Open GitHub PR](https://github.com/owner/repo/pull/41)" in message
    assert "[Open GitHub PR](https://github.com/owner/repo/pull/40)" not in message


def test_http_github_pr_url_is_normalized_to_https(tmp_path):
    config_path = write_config(tmp_path)

    def runner(args, env=None):
        if "list" in args:
            return completed_json(args, [{"id": "t_http", "title": "HTTP PR"}])
        if "show" in args:
            return completed_json(
                args,
                {"task": {"id": "t_http", "title": "HTTP PR"}, "comments": [{"created_at": 1, "body": "http://github.com/owner/repo/pull/22"}]},
            )
        raise AssertionError(args)

    message = run_once(config_path, runner=runner, now=10)

    assert "[Open GitHub PR](https://github.com/owner/repo/pull/22)" in message
    assert "http://github.com/owner/repo/pull/22" not in message


def test_run_pr_scan_ignores_unrelated_prompt_fields(tmp_path):
    config_path = write_config(tmp_path)

    def runner(args, env=None):
        if "list" in args:
            return completed_json(args, [{"id": "t_run_fields", "title": "Run fields"}])
        if "show" in args:
            return completed_json(
                args,
                {
                    "task": {"id": "t_run_fields", "title": "Run fields"},
                    "runs": [
                        {
                            "ended_at": "2026-06-16T11:00:00Z",
                            "prompt": "old unrelated https://github.com/owner/repo/pull/30",
                            "metadata": {
                                "old_pr": "https://github.com/owner/repo/pull/30",
                                "handoff": {"pr_url": "https://github.com/owner/repo/pull/31"},
                            },
                        }
                    ],
                },
            )
        raise AssertionError(args)

    message = run_once(config_path, runner=runner, now=10)

    assert "[Open GitHub PR](https://github.com/owner/repo/pull/31)" in message
    assert "[Open GitHub PR](https://github.com/owner/repo/pull/30)" not in message


def test_show_failure_falls_back_to_core_blocked_notification(tmp_path):
    config_path = write_config(tmp_path)

    def runner(args, env=None):
        if "list" in args:
            return completed_json(args, [{"id": "t_show_fail", "title": "Show fails", "result": "needs human input"}])
        if "show" in args:
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="card changed")
        raise AssertionError(args)

    message = run_once(config_path, runner=runner, now=10)

    assert "🟢 **Kanban ready — needs human input**" in message
    assert "`t_show_fail`" in message
    assert "`t_show_fail`" in message
    assert "Open GitHub PR" not in message
    assert read_state(config_path)["notified"]["default:t_show_fail"]["notified_at"] == 10


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
    assert "🟢 **Kanban ready — CLI notification**" in stdout.getvalue()
    assert "`t_cli`" in stdout.getvalue()
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

    assert message.startswith("🟢 **Kanban ready — First blocked task**")
    assert message.count("🟢 **Kanban ready") == 2
    assert "t_one" in message
    assert "t_two" in message
    assert "Tip: unblock or comment from the Kanban dashboard when ready." not in message


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


def test_shared_state_preserves_same_board_entries_from_other_filter_scopes(tmp_path):
    state_path = tmp_path / "shared-state.json"
    alice_dir = tmp_path / "alice"
    bob_dir = tmp_path / "bob"
    alice_dir.mkdir()
    bob_dir.mkdir()
    alice_config = write_config(alice_dir, assignee="alice", state_path=str(state_path))
    bob_config = write_config(bob_dir, assignee="bob", state_path=str(state_path))

    first_alice = run_once(alice_config, runner=fake_runner([{"id": "t_alice", "title": "Alice task"}]), now=10)
    first_bob = run_once(bob_config, runner=fake_runner([{"id": "t_bob", "title": "Bob task"}]), now=20)
    second_alice = run_once(alice_config, runner=fake_runner([{"id": "t_alice", "title": "Alice task"}]), now=30)

    assert "t_alice" in first_alice
    assert "t_bob" in first_bob
    assert second_alice == ""
    assert read_state(alice_config)["notified"] == {
        "default|assignee=alice:t_alice": {
            "task_id": "t_alice",
            "board": "default",
            "scope": "default|assignee=alice",
            "notified_at": 10,
            "last_seen_blocked_at": 30,
        },
        "default|assignee=bob:t_bob": {
            "task_id": "t_bob",
            "board": "default",
            "scope": "default|assignee=bob",
            "notified_at": 20,
            "last_seen_blocked_at": 20,
        },
    }


def test_shared_state_preserves_same_board_entries_from_other_status_scopes(tmp_path):
    state_path = tmp_path / "shared-state.json"
    blocked_dir = tmp_path / "blocked"
    review_dir = tmp_path / "review"
    blocked_dir.mkdir()
    review_dir.mkdir()
    blocked_config = write_config(blocked_dir, status="blocked", state_path=str(state_path))
    review_config = write_config(review_dir, status="review", state_path=str(state_path))

    first_blocked = run_once(blocked_config, runner=fake_runner([{"id": "t_blocked", "title": "Blocked task"}]), now=10)
    first_review = run_once(review_config, runner=fake_runner([{"id": "t_review", "title": "Review task"}]), now=20)
    second_blocked = run_once(blocked_config, runner=fake_runner([{"id": "t_blocked", "title": "Blocked task"}]), now=30)

    assert "t_blocked" in first_blocked
    assert "t_review" in first_review
    assert second_blocked == ""
    assert set(read_state(blocked_config)["notified"]) == {"default:t_blocked", "default|status=review:t_review"}


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
    assert "[Open Kanban task](https://example.test/ops/tasks/t_link)" in message


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


class MultiBoardRunner:
    def __init__(self, boards=None, tasks_by_board=None, details_by_board_task=None, fail_boards=None):
        self.boards = boards or []
        self.tasks_by_board = tasks_by_board or {}
        self.details_by_board_task = details_by_board_task or {}
        self.fail_boards = fail_boards or {}
        self.calls = []

    def __call__(self, args, env=None):
        self.calls.append((args, env or {}))
        if args == ["hermes", "kanban", "boards", "list", "--json"]:
            return completed_json(args, self.boards)
        if "list" in args:
            board = args[args.index("--board") + 1]
            if board in self.fail_boards:
                return subprocess.CompletedProcess(args=args, returncode=2, stdout="", stderr=self.fail_boards[board])
            return completed_json(args, self.tasks_by_board.get(board, []))
        if "show" in args:
            board = args[args.index("--board") + 1]
            task_id = args[args.index("show") + 1]
            return completed_json(args, self.details_by_board_task.get((board, task_id), {"task": {"id": task_id}}))
        raise AssertionError(args)


def test_boards_all_discovers_non_archived_boards_and_notifies_per_board(tmp_path):
    config_path = write_config(
        tmp_path,
        boards="all",
        dashboard_url_template="http://agent:9119/kanban?board={board}&task={task_id}",
    )
    runner = MultiBoardRunner(
        boards=[
            {"slug": "default", "archived": False},
            {"slug": "psp", "archived": False},
            {"slug": "old", "archived": True},
        ],
        tasks_by_board={
            "default": [{"id": "t_same", "title": "Default blocked", "result": "default reason"}],
            "psp": [{"id": "t_same", "title": "PSP blocked", "result": "psp reason"}],
            "old": [{"id": "t_archived", "title": "Should not poll"}],
        },
    )

    message = run_once(config_path, runner=runner, now=10)

    assert message.count("🟢 **Kanban ready") == 2
    assert "Board: `default`" in message
    assert "Board: `psp`" in message
    assert "board=default&task=t_same" in message
    assert "board=psp&task=t_same" in message
    assert "old" not in [call[0][call[0].index("--board") + 1] for call in runner.calls if "--board" in call[0]]
    assert set(read_state(config_path)["notified"]) == {"default:t_same", "psp:t_same"}


def test_explicit_boards_array_polls_each_board_with_filters_without_discovery(tmp_path):
    config_path = write_config(tmp_path, boards=["default", "psp"], status="review", assignee="coder", tenant="customer-a")
    runner = MultiBoardRunner(tasks_by_board={"default": [], "psp": []})

    run_once(config_path, runner=runner, now=10)

    assert [call[0] for call in runner.calls] == [
        [
            "hermes",
            "kanban",
            "--board",
            "default",
            "list",
            "--status",
            "review",
            "--json",
            "--assignee",
            "coder",
            "--tenant",
            "customer-a",
        ],
        [
            "hermes",
            "kanban",
            "--board",
            "psp",
            "list",
            "--status",
            "review",
            "--json",
            "--assignee",
            "coder",
            "--tenant",
            "customer-a",
        ],
    ]
    assert all(call[1]["HERMES_KANBAN_BOARD"] in {"default", "psp"} for call in runner.calls)


def test_multi_board_state_cleanup_is_scoped_to_each_polled_board(tmp_path):
    config_path = write_config(tmp_path, boards=["default", "psp"])
    state_path = Path(json.loads(config_path.read_text(encoding="utf-8"))["state_path"])
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "notified": {
                    "default:t_old": {"task_id": "t_old", "board": "default", "notified_at": 1, "last_seen_blocked_at": 1},
                    "psp:t_keep": {"task_id": "t_keep", "board": "psp", "notified_at": 1, "last_seen_blocked_at": 1},
                    "ops:t_other": {"task_id": "t_other", "board": "ops", "notified_at": 1, "last_seen_blocked_at": 1},
                },
            }
        ),
        encoding="utf-8",
    )
    runner = MultiBoardRunner(tasks_by_board={"default": [], "psp": [{"id": "t_keep", "title": "Still blocked"}]})

    run_once(config_path, runner=runner, now=10)

    state = read_state(config_path)["notified"]
    assert set(state) == {"psp:t_keep", "ops:t_other"}
    assert state["psp:t_keep"]["last_seen_blocked_at"] == 10


@pytest.mark.parametrize("boards", ["default", [], ["default", ""], ["default", 3], {"slug": "default"}, None])
def test_invalid_boards_config_exits_nonzero_without_running_kanban(tmp_path, boards):
    config_path = write_config(tmp_path, boards=boards)
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
    assert "boards" in stderr.getvalue()


def test_boards_all_discovery_failure_exits_nonzero_without_updating_state(tmp_path):
    config_path = write_config(tmp_path, boards="all")
    state_path = Path(json.loads(config_path.read_text(encoding="utf-8"))["state_path"])
    original = {"version": 1, "notified": {"default:t_keep": {"task_id": "t_keep", "board": "default", "notified_at": 1, "last_seen_blocked_at": 1}}}
    state_path.write_text(json.dumps(original), encoding="utf-8")

    def runner(args, env=None):
        return subprocess.CompletedProcess(args=args, returncode=2, stdout="", stderr="boards unavailable")

    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = main(["--config", str(config_path)], runner=runner, stdout=stdout, stderr=stderr, now=10)

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert "hermes kanban boards list failed" in stderr.getvalue()
    assert json.loads(state_path.read_text(encoding="utf-8")) == original


def test_multi_board_list_failure_exits_nonzero_without_updating_state(tmp_path):
    config_path = write_config(tmp_path, boards=["default", "psp"])
    state_path = Path(json.loads(config_path.read_text(encoding="utf-8"))["state_path"])
    original = {"version": 1, "notified": {}}
    state_path.write_text(json.dumps(original), encoding="utf-8")
    runner = MultiBoardRunner(tasks_by_board={"default": []}, fail_boards={"psp": "board unavailable"})
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(["--config", str(config_path)], runner=runner, stdout=stdout, stderr=stderr, now=10)

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert "hermes kanban list failed for board 'psp'" in stderr.getvalue()
    assert json.loads(state_path.read_text(encoding="utf-8")) == original



def test_boards_all_empty_discovery_preserves_existing_state_without_polling_default(tmp_path):
    config_path = write_config(tmp_path, boards="all")
    state_path = Path(json.loads(config_path.read_text(encoding="utf-8"))["state_path"])
    original = {
        "version": 1,
        "notified": {
            "default:t_keep": {"task_id": "t_keep", "board": "default", "notified_at": 1, "last_seen_blocked_at": 1},
            "psp:t_keep": {"task_id": "t_keep", "board": "psp", "notified_at": 1, "last_seen_blocked_at": 1},
        },
    }
    state_path.write_text(json.dumps(original), encoding="utf-8")
    runner = MultiBoardRunner(boards=[])

    message = run_once(config_path, runner=runner, now=10)

    assert message == ""
    assert [call[0] for call in runner.calls] == [["hermes", "kanban", "boards", "list", "--json"]]
    assert runner.calls[0][1] == {"HERMES_KANBAN_DB": ""}
    assert read_state(config_path) == original
