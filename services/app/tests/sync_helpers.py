"""Shared helpers for the live-sync RoomManager tests."""
from __future__ import annotations

from wsi_app.sync_room import RoomManager


async def flush_audience(mgr: RoomManager, session_id: str) -> None:
    """Send a pending debounced audience frame now.

    Cancels the trailing timer RoomManager._schedule_audience armed and sends
    the coalesced state at once, so a test does not have to wait out the
    debounce window. A no-op when nothing is pending.
    """
    room = mgr.get(session_id)
    if room is None:
        return
    if room._audience_timer is not None:
        room._audience_timer.cancel()
        room._audience_timer = None
    if room._audience_pending:
        room._audience_pending = False
        await mgr._send_audience_to_presenter(room)
