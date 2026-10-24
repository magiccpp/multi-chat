import asyncio
import json
from pathlib import Path

import pytest
from multichat_client.runner.agent import Runner

from tests.conftest import TOKEN, chat, finished, models_by_name


@pytest.fixture
async def runner(server, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "hello.py").write_text("print('hello')\n")
    states: list[tuple[str, str]] = []
    r = Runner(server, TOKEN, [str(tmp_path)], name=f"test-{tmp_path.name}", on_status=lambda s, d: states.append((s, d)))
    task = asyncio.create_task(r.run_forever())
    for _ in range(100):
        if r.runner_id:
            break
        await asyncio.sleep(0.05)
    assert r.runner_id, states
    yield r, root
    await r.stop()
    task.cancel()


async def build_session(client, runner_id: int, root: Path, approve_commands="ask") -> dict:
    models = await models_by_name(client)
    alpha = models["mock-alpha"]["id"]
    s = (
        await client.post(
            "/api/sessions",
            json={"title": "build", "model_ids": [alpha], "workspace": {"kind": "runner", "runner_id": runner_id, "path": str(root)}},
        )
    ).json()
    await client.post(f"/api/sessions/{s['id']}/pick-builder", json={"model_id": alpha})
    await client.patch(f"/api/sessions/{s['id']}", json={"approve_commands": approve_commands})
    return s


async def test_runner_registers_and_browses(client, runner):
    r, root = runner
    listed = {x["name"]: x for x in (await client.get("/api/runners")).json()}
    assert listed[r.name]["online"] is True
    top = (await client.get(f"/api/runners/{r.runner_id}/browse")).json()
    assert len(top["entries"]) == 1
    inside = (await client.get(f"/api/runners/{r.runner_id}/browse", params={"path": top["entries"][0]["name"]})).json()
    assert "proj" in [e["name"] for e in inside["entries"]]
    outside = await client.get(f"/api/runners/{r.runner_id}/browse", params={"path": "/etc"})
    assert outside.status_code == 400


async def test_plan_reads_but_cannot_write(client, runner):
    r, root = runner
    models = await models_by_name(client)
    s = (
        await client.post(
            "/api/sessions",
            json={
                "title": "plan-tools",
                "model_ids": [models["mock-beta"]["id"]],
                "workspace": {"kind": "runner", "runner_id": r.runner_id, "path": str(root)},
            },
        )
    ).json()
    prompt = 'Look around\n@tool list_dir {"path": "."}\n@tool write_file {"path": "x.txt", "content": "no"}'
    events = await chat(client, s["id"], prompt)
    results = [e["message"]["content"] for e in events if e["type"] == "tool_result"]
    assert len(results) == 1 and "hello.py" in results[0]  # write_file is not offered in PLAN
    assert not (root / "x.txt").exists()
    tree = (await client.get(f"/api/sessions/{s['id']}/workspace/tree")).json()
    assert [e["name"] for e in tree["entries"]] == ["hello.py"]
    assert (await client.get(f"/api/sessions/{s['id']}/workspace/file", params={"path": "hello.py"})).json()["content"] == "print('hello')\n"


async def test_plan_runs_read_only_commands_and_refuses_changes(client, runner):
    r, root = runner
    models = await models_by_name(client)
    s = (
        await client.post(
            "/api/sessions",
            json={"title": "plan-commands", "model_ids": [models["mock-beta"]["id"]],
                  "workspace": {"kind": "runner", "runner_id": r.runner_id, "path": str(root)}},
        )
    ).json()
    assert s["approve_commands"] == "ask"  # BUILD would ask; read-only commands in PLAN just run
    prompt = "\n".join([
        "Check things",
        '@tool run_command {"command": "cat hello.py && ls -la | grep hello"}',
        '@tool run_command {"command": "touch made-in-plan"}',
        '@tool run_command {"command": "echo x > out.txt"}',
    ])
    events = await chat(client, s["id"], prompt)
    assert not [e for e in events if e["type"] == "approval_required"]
    results = [e["message"]["content"] for e in events if e["type"] == "tool_result"]
    assert "print('hello')" in results[0] and "exit code 0" in results[0]
    assert results[1].startswith("Refused in PLAN") and "touch" in results[1]
    assert results[2].startswith("Refused in PLAN") and "redirect" in results[2]
    assert not (root / "made-in-plan").exists() and not (root / "out.txt").exists()


async def test_plan_runs_a_replys_commands_in_parallel(client, runner):
    import time

    r, root = runner
    models = await models_by_name(client)
    s = (await client.post("/api/sessions", json={"title": "plan-parallel", "model_ids": [models["mock-beta"]["id"]],
                                                  "workspace": {"kind": "runner", "runner_id": r.runner_id, "path": str(root)}})).json()
    prompt = "\n".join([
        "Two slow checks",
        '@tool run_command {"command": "sleep 1.5 && echo first"}',
        '@tool run_command {"command": "sleep 1.5 && echo second"}',
    ])
    started = time.monotonic()
    events = await chat(client, s["id"], prompt)
    elapsed = time.monotonic() - started
    results = [e["message"]["content"] for e in events if e["type"] == "tool_result"]
    assert "first" in results[0] and "second" in results[1]  # stored in the order the model asked
    assert elapsed < 2.8, f"took {elapsed:.1f}s: the commands ran one after another"


async def test_project_memory_file_in_the_folder(client, runner):
    r, root = runner
    (root / "CLAUDE.md").write_text("# Project rules\n- Use tabs\n")
    models = await models_by_name(client)
    s = (await client.post("/api/sessions", json={"title": "project-memory", "model_ids": [models["mock-beta"]["id"]],
                                                  "workspace": {"kind": "runner", "runner_id": r.runner_id, "path": str(root)}})).json()
    reply = finished(await chat(client, s["id"], "hi"))[0]
    assert "(project instructions: CLAUDE.md)" in reply["content"]
    (root / "CLAUDE.md").unlink()


async def test_build_edits_runs_command_with_approval_and_reverts(client, runner):
    r, root = runner
    s = await build_session(client, r.runner_id, root)
    prompt = "\n".join(
        [
            "Implement it",
            '@tool edit_file {"path": "hello.py", "old_string": "hello", "new_string": "hello world"}',
            '@tool write_file {"path": "src/new.txt", "content": "fresh\\n"}',
            '@tool run_command {"command": "echo ran-$((40+2)) && cat hello.py"}',
            '@tool read_file {"path": "../../etc/passwd"}',
        ]
    )

    async def approver():
        async with client.stream("GET", f"/api/sessions/{s['id']}/events") as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    ev = json.loads(line[6:])
                    if ev["type"] == "approval_required":
                        assert ev["kind"] == "command" and "echo ran" in ev["command"]
                        await client.post(f"/api/sessions/{s['id']}/approvals/{ev['call_id']}", json={"approve": True})
                        return

    approve_task = asyncio.create_task(approver())
    await asyncio.sleep(0.2)
    events = await chat(client, s["id"], prompt)
    await asyncio.wait_for(approve_task, 5)

    results = {e["call_id"]: e["message"]["content"] for e in events if e["type"] == "tool_result"}
    contents = list(results.values())
    assert any(c.startswith("Updated hello.py") for c in contents)
    assert any(c.startswith("Created src/new.txt") for c in contents)
    assert any("[exit code 0]" in c and "ran-42" in c and "hello world" in c for c in contents)
    assert any("outside the workspace" in c for c in contents)
    assert any(e["type"] == "tool_progress" and "ran-42" in e["text"] for e in events)
    assert (root / "hello.py").read_text() == "print('hello world')\n"
    assert (root / "src/new.txt").read_text() == "fresh\n"
    assert "Done. Tool results" in finished(events)[-1]["content"]

    changes = (await client.get(f"/api/sessions/{s['id']}/changes")).json()
    assert {c["path"] for c in changes} == {"hello.py", "src/new.txt"}
    reverted = (await client.post(f"/api/sessions/{s['id']}/revert", json={})).json()
    assert set(reverted["restored"]) == {"hello.py", "src/new.txt"}
    assert (root / "hello.py").read_text() == "print('hello')\n"
    assert not (root / "src/new.txt").exists()


async def test_rejected_command_is_reported_to_model(client, runner):
    r, root = runner
    s = await build_session(client, r.runner_id, root)

    async def rejecter():
        async with client.stream("GET", f"/api/sessions/{s['id']}/events") as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: ") and json.loads(line[6:])["type"] == "approval_required":
                    ev = json.loads(line[6:])
                    await client.post(
                        f"/api/sessions/{s['id']}/approvals/{ev['call_id']}", json={"approve": False, "note": "not now"}
                    )
                    return

    task = asyncio.create_task(rejecter())
    await asyncio.sleep(0.2)
    events = await chat(client, s["id"], 'Do it\n@tool run_command {"command": "touch should-not-exist"}')
    await asyncio.wait_for(task, 5)
    result = next(e["message"]["content"] for e in events if e["type"] == "tool_result")
    assert "rejected" in result and "not now" in result
    assert not (root / "should-not-exist").exists()


async def test_stop_cancels_long_command(client, runner):
    r, root = runner
    s = await build_session(client, r.runner_id, root, approve_commands="auto")

    async def stopper():
        await asyncio.sleep(1.0)
        await client.post(f"/api/sessions/{s['id']}/stop")

    task = asyncio.create_task(stopper())
    events = await chat(client, s["id"], 'Wait\n@tool run_command {"command": "sleep 30 && touch late"}')
    await task
    assert events[-1]["type"] == "turn_end" and events[-1]["status"] == "cancelled"
    await asyncio.sleep(0.3)
    assert not (root / "late").exists()
    # the session accepts new turns afterwards
    assert finished(await chat(client, s["id"], "still there?"))
