"""Wait tips from `status` events."""

from multichat_client.state import SHOW_AFTER, STALL_AFTER, SessionState, elapsed


def state() -> SessionState:
    st = SessionState()
    st.load({"id": 1, "participants": [{"model_id": 7}, {"model_id": 8}]}, [], [],
            [{"id": 7, "display_name": "Alpha", "color": "#f00"}, {"id": 8, "display_name": "Beta", "color": "#0f0"}])
    return st


def test_tip_appears_after_a_few_seconds_and_keeps_its_start_time():
    st = state()
    st.apply({"type": "status", "model_id": 8, "state": "waiting_model", "text": "waiting for the reply to start (openrouter)",
              "hint": "Stop cancels the turn."})
    start = st.status[8]["since"]
    assert st.waits(start + 1) == []
    [w] = st.waits(start + SHOW_AFTER + 9)
    assert w.line == "Beta: waiting for the reply to start (openrouter) · 12 s"
    assert w.hint == "Stop cancels the turn."
    # same state with new text (progress) keeps counting from the start
    st.apply({"type": "status", "model_id": 8, "state": "waiting_model", "text": "still waiting"})
    assert st.status[8]["since"] == start


def test_approval_shows_at_once_and_order_follows_participants():
    st = state()
    st.apply({"type": "status", "model_id": 8, "state": "tool", "text": "running `ls`"})
    st.apply({"type": "status", "model_id": 7, "state": "approval", "text": "waiting for your approval to run `rm x`"})
    now = st.status[7]["since"]
    assert [w.name for w in st.waits(now)] == ["Alpha"]
    assert [w.name for w in st.waits(now + SHOW_AFTER)] == ["Alpha", "Beta"]


def test_writing_only_shows_when_output_stalls():
    st = state()
    st.apply({"type": "status", "model_id": 7, "state": "writing", "text": "writing its answer"})
    st.apply({"type": "message_start", "message": {"id": 1, "seq": 1, "role": "assistant", "model_id": 7}})
    st.apply({"type": "delta", "message_id": 1, "model_id": 7, "text": "Hel"})
    last = st.status[7]["last"]
    assert st.waits(last + STALL_AFTER - 1) == []
    [w] = st.waits(last + STALL_AFTER + 5)
    assert w.state == "stalled" and w.line == "Alpha: no new output · 25 s"


def test_cleared_by_status_none_and_turn_end():
    st = state()
    st.apply({"type": "status", "model_id": 7, "state": "thinking", "text": "thinking"})
    st.apply({"type": "status", "model_id": 8, "state": "tool", "text": "x"})
    st.apply({"type": "status", "model_id": 7, "state": None})
    assert list(st.status) == [8]
    st.apply({"type": "turn_end", "status": "complete"})
    assert st.status == {}


def test_elapsed():
    assert elapsed(5.9) == "5 s"
    assert elapsed(125) == "2 min 05 s"
