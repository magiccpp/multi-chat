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
        # no folder chosen: the TUI's own machine is used, so models can inspect it
        await until(pilot, lambda: app.st.session.get("workspace_kind") == "runner")
        assert app.st.session["workspace_path"] == str(root.resolve())

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

        await send(pilot, app, "/cost")
        await until(pilot, lambda: isinstance(app.screen, TextScreen))
        await pilot.pause(0.2)
        app.save_screenshot(str(SHOTS / "tui-cost.svg"))
        await pilot.press("escape")
        await pilot.pause(0.1)

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


async def test_tui_models_new_session_and_attach(server, client, tmp_path):
    from PIL import Image
    from textual.widgets import Input, SelectionList

    from multichat_tui import cli
    from multichat_tui.screens import ModelsScreen

    await client.post("/api/endpoints/check")  # demo models: context length and effort support
    ep = (await client.post("/api/endpoints", json={"name": "tui-nokey", "kind": "openai_compatible",
                                                    "base_url": "https://api.example.com/v1"})).json()
    gone = (await client.post("/api/models", json={"endpoint_id": ep["id"], "model_name": "needs-key", "display_name": "Needs key"})).json()
    await client.post(f"/api/endpoints/{ep['id']}/check")
    models = await models_by_name(client)
    s = (await client.post("/api/sessions", json={"title": "Attach", "model_ids": [models["mock-alpha"]["id"]]})).json()
    doc = tmp_path / "spec.md"
    doc.write_text("# Spec\nTokens last 15 minutes.\n")
    png = tmp_path / "screen.png"
    Image.new("RGB", (64, 48), "red").save(png)

    api = MultiChatClient(server, TOKEN)
    app = MultiChatApp(api, ClientConfig(url=server, token=TOKEN), session_id=s["id"], start_runner=False)
    async with app.run_test(size=(150, 46)) as pilot:
        await until(pilot, lambda: app.st.session_id == s["id"])

        # unavailable models are greyed out (disabled) in /models; typing filters the list
        await send(pilot, app, "/models")
        await until(pilot, lambda: isinstance(app.screen, ModelsScreen))
        lst = app.screen.query_one(SelectionList)
        options = {lst.get_option_at_index(i).value: lst.get_option_at_index(i) for i in range(lst.option_count)}
        assert options[gone["id"]].disabled and not options[models["mock-beta"]["id"]].disabled
        app.screen.query_one(Input).value = "mock-beta"
        await pilot.pause(0.1)
        assert [lst.get_option_at_index(i).value for i in range(lst.option_count)] == [models["mock-beta"]["id"]]
        await pilot.press("escape")

        # effort per model (any time); the sidebar shows the model's context window and effort
        await send(pilot, app, "/effort Alpha high")
        await until(pilot, lambda: (app.st.participant(models["mock-alpha"]["id"]) or {}).get("effort") == "high")
        assert "effort high" in str(app.query_one("#side-participants").render())

        # /new while the session is empty is ignored
        await send(pilot, app, "/new")
        await pilot.pause(0.2)
        assert app.st.session_id == s["id"]

        # /attach a document and an image, then send
        await send(pilot, app, f"/attach {doc} {png}")
        assert [p.name for p in app.pending_files] == ["spec.md", "screen.png"]
        await send(pilot, app, "What do these show?")
        await until(pilot, lambda: not app.st.busy and any(m.role == "assistant" and m.status == "complete" for m in app.st.messages.values()))
        assert not app.pending_files
        user = next(m for m in app.st.ordered() if m.role == "user")
        assert [a["filename"] for a in user.attachments] == ["spec.md", "screen.png"]
        reply = next(m for m in app.st.ordered() if m.role == "assistant")
        assert "(I see 1 image.)" in reply.content and "spec.md" in reply.content
        assert "(effort: high)" in reply.content
        await pilot.pause(0.2)
        app.save_screenshot(str(SHOTS / "tui-attachments.svg"))

        # Claude Code-style memory and session commands
        await client.put("/api/memory", json={"text": ""})
        await send(pilot, app, "# deploy target is nuc2")
        await pilot.pause(0.3)
        assert (await client.get("/api/memory")).json()["text"] == "- deploy target is nuc2"
        await send(pilot, app, "/memory")
        await until(pilot, lambda: isinstance(app.screen, TextScreen))
        await pilot.press("escape")
        await send(pilot, app, "Follow-up")
        await until(pilot, lambda: not app.st.busy and len([m for m in app.st.messages.values() if m.role == "assistant"]) == 2)
        assert "(memory: 1 items)" in [m for m in app.st.ordered() if m.role == "assistant"][-1].content
        await send(pilot, app, "/context")
        await until(pilot, lambda: isinstance(app.screen, TextScreen))
        await pilot.press("escape")
        await send(pilot, app, "/compact")
        await until(pilot, lambda: any(m.role == "summary" for m in app.st.messages.values()))
        await client.put("/api/memory", json={"text": ""})

        # with messages in it, /new creates a session; a second /new right away is ignored
        await send(pilot, app, "/new")
        await until(pilot, lambda: app.st.session_id != s["id"])
        fresh = app.st.session_id
        await send(pilot, app, "/new")
        await pilot.pause(0.2)
        assert app.st.session_id == fresh
    await api.close()

    # plain CLI: multichat run --attach
    code = await asyncio.to_thread(cli.main, ["--url", server, "--token", TOKEN, "run", "Summarise the spec", "--models", "Alpha",
                                              "--attach", str(doc), "--json"])
    assert code == 0
    latest = (await client.get("/api/sessions", params={"limit": 1})).json()[0]
    msgs = (await client.get(f"/api/sessions/{latest['id']}/messages")).json()
    assert msgs[0]["attachments"][0]["filename"] == "spec.md" and "spec.md" in msgs[1]["content"]
