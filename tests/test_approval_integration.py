"""Approval cards against the installed Hermes queue, worker and Teams SDK.

Only transport is mocked. Run via scripts/test_with_hermes.py; no live posts.
"""
from __future__ import annotations

import contextvars
import os
import queue
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

if not os.environ.get("HERMES_TEST_SOURCE"):
    pytest.skip("Use scripts/test_with_hermes.py with the actual Hermes runtime", allow_module_level=True)

from test_adapter_integration import (  # reuse real SDK/home/network fixtures
    THREAD, adapter, context, isolated_home_and_no_network,
)
from hermes_teams import adapter as adapter_module
from gateway.platforms.base import SendResult
from gateway.session_context import set_session_vars, clear_session_vars
from tools import approval
from tools.approval_gateway_wait import _await_gateway_decision


@pytest.fixture
def waiting():
    """Real blocking approval workers; bounded cleanup even on a failed assertion."""
    workers = []

    def start(session="teams:test-session", command="echo approval-test", requester="human-aad", guard=False):
        notified = queue.Queue()
        finished = queue.Queue()
        copied = contextvars.copy_context()

        def worker():
            tokens = set_session_vars(platform="teams", chat_id=THREAD, chat_type="group",
                                      actor_id=requester, actor_name="Requester", session_key=session)
            try:
                if guard:
                    approval.register_gateway_notify(session, notified.put)
                    finished.put(approval.check_dangerous_command(command, "local"))
                else:
                    finished.put(_await_gateway_decision(session, notified.put, {
                        "command": command, "description": "Test consent", "pattern_key": command,
                    }))
            except BaseException as error:
                finished.put(error)
            finally:
                if guard:
                    approval.unregister_gateway_notify(session)
                clear_session_vars(tokens)

        thread = threading.Thread(target=lambda: copied.run(worker), daemon=True)
        workers.append((thread, session))
        thread.start()
        data = notified.get(timeout=5)
        assert thread.is_alive()
        return SimpleNamespace(thread=thread, data=data, session=session, finished=finished)

    yield start
    for thread, session in workers:
        approval.resolve_gateway_approval(session, "deny", resolve_all=True)
        thread.join(timeout=5)
        assert not thread.is_alive(), "Approval worker leaked"


async def render(adapter, pending, **kwargs):
    assert adapter_module.check_teams_requirements()
    adapter._send_card = AsyncMock(return_value=SimpleNamespace(id="approval-card"))
    result = await adapter.send_exec_approval(
        THREAD, pending.data["command"], pending.session, description=pending.data["description"],
        request_id=pending.data["request_id"], allow_session=False, allow_permanent=False, **kwargs)
    assert result.success
    sent = adapter._send_card.await_args
    assert sent is not None
    card = sent.args[1].model_dump(by_alias=True, exclude_none=True)
    return card


def action(card, verb="approve_once"):
    return next(dict(row["data"]) for row in card["actions"] if row["data"]["hermes_action"] == verb)


def click(data, *, actor="human-aad", chat=THREAD):
    ctx = context(thread=chat, attachments=[])
    ctx.activity.from_.aad_object_id = actor
    ctx.activity.value = SimpleNamespace(action=SimpleNamespace(data=data))
    return ctx


def outcome(pending, choice):
    result = pending.finished.get(timeout=5)
    assert not isinstance(result, BaseException), repr(result)
    assert result["resolved"] is True and result["choice"] == choice
    pending.thread.join(timeout=5)
    assert not pending.thread.is_alive()


@pytest.mark.asyncio
async def test_approval_card_binds_request_and_keeps_preview_on_server(adapter, waiting):
    pending = waiting()
    card = await render(adapter, pending)
    data = action(card)
    assert data.get("request_id") == pending.data["request_id"]
    assert data.get("prompt_id")
    assert "cmd" not in data and "desc" not in data
    assert pending.data["command"] in str(card["body"])


@pytest.mark.asyncio
@pytest.mark.parametrize("obsolete", ["stale", "missing"])
async def test_obsolete_card_cannot_approve_next_waiting_request(adapter, waiting, obsolete):
    first = waiting(command="echo first")
    old = action(await render(adapter, first))
    await adapter._on_card_action(click(old))
    outcome(first, "once")
    next_request = waiting(command="echo second")
    await render(adapter, next_request)
    if obsolete == "missing":
        old.pop("request_id")
    await adapter._on_card_action(click(old))
    assert approval.has_blocking_approval(next_request.session)
    assert next_request.finished.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["requester", "slash"])
async def test_admitted_but_unauthorized_actor_cannot_approve(adapter, waiting, isolated_home_and_no_network, policy):
    if policy == "requester":
        (isolated_home_and_no_network / "config.yaml").write_text("approvals:\n  approvers: []\n")
    else:
        adapter.config.extra["group_allow_admin_from"] = ["admin-aad"]
    pending = waiting()
    authority = approval.list_gateway_approval_authority(pending.session)[0]
    assert authority["requester_id"] == "human-aad"
    assert authority["approvers"] == ([] if policy == "requester" else ["*"])
    data = action(await render(adapter, pending))
    await adapter._on_card_action(click(data, actor="other-admitted-aad"))
    assert approval.has_blocking_approval(pending.session)
    assert pending.finished.empty()
    allowed = "human-aad" if policy == "requester" else "admin-aad"
    await adapter._on_card_action(click(data, actor=allowed))
    outcome(pending, "once")


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["conversation", "session-and-request", "request", "prompt", "choice"])
async def test_tampered_card_cannot_answer_another_context(adapter, waiting, tamper):
    first = waiting(command="echo first")
    first_data = action(await render(adapter, first))
    other = waiting(session="teams:other-session" if tamper == "session-and-request" else first.session,
                    command="echo other")
    other_data = action(await render(adapter, other))
    data = dict(first_data)
    chat = THREAD
    if tamper == "conversation":
        chat = "19:unrelated@thread.tacv2"
    elif tamper == "session-and-request":
        data.update(session_key=other.session, request_id=other_data["request_id"])
    elif tamper == "request":
        data["request_id"] = other_data["request_id"]
    elif tamper == "prompt":
        data.pop("prompt_id")
    else:
        data["hermes_action"] = "approve_session"  # this prompt offered once/deny only
    await adapter._on_card_action(click(data, chat=chat))
    remaining = {row["request_id"] for session in {first.session, other.session}
                 for row in approval.list_gateway_approvals(session)}
    assert remaining == {first.data["request_id"], other.data["request_id"]}
    assert first.finished.empty() and other.finished.empty()
    assert approval.has_blocking_approval(first.session)
    assert approval.has_blocking_approval(other.session)


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["complete", "failed", "unsafe-fence"])
async def test_long_command_requires_full_delivery_and_explains_shortened_preview(adapter, waiting, delivery):
    command = "echo " + "x" * 3000 + ("```tail" if delivery == "unsafe-fence" else " tail")
    pending = waiting(command=command)
    adapter.send = AsyncMock(return_value=SendResult(success=delivery == "complete"))
    card = await render(adapter, pending)
    choices = [row["data"]["hermes_action"] for row in card["actions"]]
    assert choices == (["approve_once", "deny"] if delivery == "complete" else ["deny"])
    notice = adapter_module.t("gateway.exec_approval.truncated_" + (
        "full_above" if delivery == "complete" else "deny_only"), total=len(command))
    assert notice in [block.get("text") for block in card["body"]]
    if delivery == "unsafe-fence":
        adapter.send.assert_not_awaited()
    else:
        args = adapter.send.await_args
        assert args is not None
        assert args.args == (THREAD, f"```\n{command}\n```")
        assert args.kwargs["metadata"]["_interim_send"] is True
    if delivery != "complete":
        forged = action(card, "deny")
        forged["hermes_action"] = "approve_once"
        await adapter._on_card_action(click(forged))
        assert approval.has_blocking_approval(pending.session)
    await adapter._on_card_action(click(action(card, "approve_once" if delivery == "complete" else "deny")))
    outcome(pending, "once" if delivery == "complete" else "deny")


@pytest.mark.asyncio
async def test_once_resumes_only_selected_worker_and_deny_stops_other(adapter, waiting):
    first = waiting(command="echo first")
    first_card = await render(adapter, first)
    second = waiting(command="echo second")
    second_card = await render(adapter, second)
    data = action(second_card)
    data.update(cmd="forged-command", desc="forged-reason")
    response = await adapter._on_card_action(click(data))
    outcome(second, "once")
    assert first.finished.empty()
    assert [p["request_id"] for p in approval.list_gateway_approvals(first.session)] == [first.data["request_id"]]
    resolved = response.model_dump(by_alias=True, exclude_none=True)
    assert "echo second" in str(resolved) and "Test consent" in str(resolved)
    assert "forged-command" not in str(resolved) and "forged-reason" not in str(resolved)
    await adapter._on_card_action(click(action(first_card, "deny")))
    outcome(first, "deny")
    assert not approval.has_blocking_approval(first.session)
    assert approval.is_approved(first.session, "echo second") is False  # no session/permanent grant


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["default-deny", "allowlist"])
async def test_existing_teams_admission_gate_still_denies_unadmitted_actors(adapter, waiting, monkeypatch, gate):
    pending = waiting()
    card = await render(adapter, pending)
    monkeypatch.delenv("TEAMS_ALLOW_ALL_USERS", raising=False)
    if gate == "allowlist":
        monkeypatch.setenv("TEAMS_ALLOWED_USERS", "someone-else")
    await adapter._on_card_action(click(action(card)))
    assert approval.has_blocking_approval(pending.session)
    assert pending.finished.empty()


@pytest.mark.asyncio
async def test_bounded_preview_eviction_fails_closed(adapter, waiting):
    adapter._EA_PREVIEWS_KEPT = 1
    first = waiting(command="echo first")
    first_data = action(await render(adapter, first))
    second = waiting(command="echo second")
    second_data = action(await render(adapter, second))
    assert len(adapter._approval_previews) == 1
    await adapter._on_card_action(click(first_data))
    assert first.finished.empty()
    assert len(approval.list_gateway_approvals(first.session)) == 2
    await adapter._on_card_action(click(second_data))
    outcome(second, "once")


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["approve_once", "deny"])
async def test_public_command_guard_returns_consent_or_blocked(adapter, waiting, isolated_home_and_no_network, choice):
    # Manual applies ONLY in the throwaway test profile; the production policy is untouched.
    (isolated_home_and_no_network / "config.yaml").write_text("approvals:\n  mode: manual\n")
    pending = waiting(command="git reset --hard", guard=True)
    card = await render(adapter, pending)
    await adapter._on_card_action(click(action(card, choice)))
    result = pending.finished.get(timeout=5)
    assert not isinstance(result, BaseException), repr(result)
    assert result["approved"] is (choice == "approve_once")
    if choice == "deny":
        assert result["outcome"] == "denied" and result["user_consent"] is False
        assert result["message"].startswith("BLOCKED:")
    pending.thread.join(timeout=5)
    assert not pending.thread.is_alive()
    assert not approval.has_blocking_approval(pending.session)
