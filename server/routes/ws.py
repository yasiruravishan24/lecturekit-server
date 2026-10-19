"""The WebSocket of one logged-in client (events.py). The first message must be {"token": "<login token>"}: the token is not put in
the address, so it never reaches a log."""
import asyncio
import json

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from ..models import User
from ..security import decode_token

router = APIRouter(tags=["realtime"])
PING_SECONDS = 25


@router.websocket("/ws")
async def socket(ws: WebSocket):
    app = ws.app
    await ws.accept()
    try:
        first = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=10))
        payload = decode_token(app.state.settings, str(first.get("token", "")))
        if payload.get("typ"):
            raise ValueError("not a login token")
        user_id = int(payload["sub"])
        with app.state.session_factory() as db:
            if db.scalar(select(User.id).where(User.id == user_id, User.is_active.is_(True))) is None:
                raise ValueError("no such user")
    except (asyncio.TimeoutError, ValueError, KeyError, TypeError, jwt.PyJWTError, WebSocketDisconnect):
        await ws.close(code=4401)                      # not logged in
        return
    hub = app.state.hub
    queue = hub.add(user_id)
    try:
        await ws.send_json({"type": "hello"})
        reader = asyncio.ensure_future(ws.receive_text())      # only to notice that the client went away
        while True:
            getter = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait({reader, getter}, timeout=PING_SECONDS, return_when=asyncio.FIRST_COMPLETED)
            if reader in done:
                getter.cancel()
                reader.result()                         # raises WebSocketDisconnect when the client closed
                reader = asyncio.ensure_future(ws.receive_text())
            if getter in done:
                await ws.send_json(getter.result())
            elif not done:
                getter.cancel()
                await ws.send_json({"type": "ping"})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.remove(user_id, queue)
