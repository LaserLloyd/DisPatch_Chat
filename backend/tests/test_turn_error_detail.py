"""A turn error's ``detail`` (the provider's address and what it said, llm_api.ApiError) is the
Admin's to read: the frame scoper drops it for every other profile, the shared landing
included, and keeps the short message (SPEC §5: connections and keys are the Admin's)."""
from __future__ import annotations

from app import main, ownership, policy


def _who(role: str, pid: str):
    return policy.Principal(kind="profile", role=role, profile_id=pid, name=pid,
                            permissions=frozenset({"use_chat"}))


def test_a_turn_error_reaches_a_non_admin_without_its_detail():
    frame = {"type": "error", "thread_id": "t-err-1", "message": "Could not reach the provider",
             "detail": "Nothing answered at http://192.0.2.44:1234/v1/chat/completions."}
    for role in ("user", "basic"):
        ownership.remember("t-err-1", f"{role}-1")
        out = main.scope_frame_for_principal(frame, _who(role, f"{role}-1"))
        assert out is not None and out["message"] == frame["message"], role
        assert "detail" not in out and "192.0.2.44" not in str(out), role
    ownership.remember("t-err-1", "admin-1")
    out = main.scope_frame_for_principal(frame, _who("admin", "admin-1"))
    assert out["detail"] == frame["detail"]                   # an Admin's own chat: the detail
    assert "detail" in frame                                   # the shared frame is not edited
