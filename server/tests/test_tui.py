import asyncio
import os
from pathlib import Path

from multichat_client.client import MultiChatClient
from multichat_client.config import ClientConfig
from multichat_tui.app import MultiChatApp
from multichat_tui.screens import ApprovalScreen, TextScreen
from multichat_tui.widgets import MessageWidget, PromptArea

from tests.conftest import TOKEN, models_by_name

SHOTS = Path(os.environ.get("MULTICHAT_SCREENSHOTS", "/tmp/multichat-screens"))


async def until(pilot, cond, timeout=10.0):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await pilot.pause(0.05)
    raise AssertionError("condition not reached")


async def send(pilot, app, text):
    prompt = app.query_one(PromptArea)
    prompt.focus()
    prompt.load_text(text)
    await pilot.press("enter")


async def test_tui_plan_vote_build(server, client, tmp_path):
    SHOTS.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "work"
    root.mkdir()
    (root / "app.py").write_text("print('v1')\n")
    models = await models_by_name(client)
    ids = [models[n]["id"] for n in ("mock-alpha", "mock-beta", "mock-gamma")]
    s = (await client.post("/api/sessions", json={"title": "Fix login bug", "model_ids": ids})).json()

    api = MultiChatClient(server, TOKEN)
    app = MultiChatApp(api, ClientConfig(url=server, token=TOKEN, runner_name=f"tui-{tmp_path.name}"), session_id=s["id"], runner_roots=[str(root)])
    async with app.run_test(size=(150, 46)) as pilot:
        await until(pilot, lambda: app.st.session_id == s["id"] and app.runner_state == "online")

        # PLAN: one message fans out to three models
        await send(pilot, app, "Users get logged out after refresh. Where is the bug?")
        await until(pilot, lambda: not app.st.busy and len([m for m in app.st.messages.values() if m.role == "assistant"]) == 3)
        await pilot.pause(0.2)
        assert app.quickvote_ids and app.query_one("#quickvote").has_class("visible")
        app.save_screenshot(str(SHOTS / "tui-plan.svg"))

        # quick vote: "2" upvotes the second reply
        offered = list(app.quickvote_ids)
        second = app.st.messages[offered[1]]
        await pilot.press("2")
        await until(pilot, lambda: app.st.messages[second.id].vote == 1)

        # /vote #n down on the third reply
        third = app.st.messages[offered[2]]
        assert not app.query_one("#quickvote").has_class("visible")
        await send(pilot, app, f"/vote #{third.seq} down")
        await until(pilot, lambda: app.st.messages[third.id].vote == -1)

        # keyboard: alt+up focuses the last reply, ↑ moves, + upvotes
        await pilot.press("alt+up")
        assert isinstance(app.focused, MessageWidget)
        await pilot.press("up")
        focused = app.focused
        await pilot.press("plus")
        await until(pilot, lambda: app.st.messages[focused.message_id].vote == 1)

        # model tabs filter the log
        await pilot.press("tab")
        await pilot.pause(0.1)
        visible = [w for w in app.widgets.values() if w.display]
        assert all(w.view.role == "user" or w.view.model_id == ids[0] for w in visible)
        await pilot.press("shift+tab")

        # b on a focused reply picks that model as builder -> BUILD
        app.widgets[second.id].focus()
        await pilot.press("b")
        await until(pilot, lambda: app.st.session.get("phase") == "build")
        assert app.st.session["builder_model_id"] == second.model_id

        # attach this machine's folder through the TUI's own runner, then build with an approval
        await send(pilot, app, f"/folder {root}")
        await until(pilot, lambda: app.st.session.get("workspace_kind") == "runner")
        await send(
            pilot,
            app,
            "Apply the fix\n"
            '@tool edit_file {"path": "app.py", "old_string": "v1", "new_string": "v2"}\n'
            '@tool run_command {"command": "cat app.py"}',
        )
        await until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
        app.save_screenshot(str(SHOTS / "tui-approval.svg"))
        await pilot.press("a")
        await until(pilot, lambda: not app.st.busy and (root / "app.py").read_text() == "print('v2')\n")
        await pilot.pause(0.3)
        assert any("v2" in r.content for r in app.st.results.values())
        app.save_screenshot(str(SHOTS / "tui-build.svg"))

        await send(pilot, app, "/revert")
        await until(pilot, lambda: (root / "app.py").read_text() == "print('v1')\n")

        await send(pilot, app, "/stats")
        await until(pilot, lambda: isinstance(app.screen, TextScreen))
        await pilot.pause(0.2)
        app.save_screenshot(str(SHOTS / "tui-stats.svg"))
        await pilot.press("escape")
        await pilot.pause(0.1)
    await api.close()

    stats = (await client.get("/api/stats")).json()
    row = {r["model_id"]: r for r in stats["models"]}
    assert row[second.model_id]["upvotes"] >= 1 and row[second.model_id]["builder_picks"] >= 1
    assert row[third.model_id]["downvotes"] >= 1
    await asyncio.sleep(0)
