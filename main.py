from __future__ import annotations
import json
import random
import string
from typing import Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from game import GameRoom

app = FastAPI(title="Rikiki")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory store
rooms: dict[str, GameRoom] = {}
# websocket_id -> (room_code, player_id)
connections: dict[str, tuple[str, str]] = {}
# (room_code, player_id) -> WebSocket
player_sockets: dict[tuple[str, str], WebSocket] = {}


def generate_room_code() -> str:
    return "".join(random.choices(string.ascii_uppercase, k=4))


async def broadcast_public_state(room: GameRoom) -> None:
    state = room.public_state()
    msg = json.dumps({"type": "game_state_public", "data": state})
    dead = []
    for player in room.players:
        key = (room.room_code, player.id)
        ws = player_sockets.get(key)
        if ws:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(key)
    for k in dead:
        player_sockets.pop(k, None)


async def send_private_state(ws: WebSocket, room: GameRoom, player_id: str) -> None:
    state = room.private_state(player_id)
    await ws.send_text(json.dumps({"type": "private_state_update", "data": state}))


async def send_to_player(room: GameRoom, player_id: str, msg: dict) -> None:
    key = (room.room_code, player_id)
    ws = player_sockets.get(key)
    if ws:
        try:
            await ws.send_text(json.dumps(msg))
        except Exception:
            pass


async def send_error(ws: WebSocket, message: str) -> None:
    await ws.send_text(json.dumps({"type": "error", "data": {"message": message}}))


async def handle_message(ws: WebSocket, data: dict, room_code: str, player_id: str) -> None:
    room = rooms.get(room_code)
    if not room:
        await send_error(ws, "Room not found")
        return

    action = data.get("action")
    payload = data.get("payload", {})

    try:
        if action == "start_game":
            seed = payload.get("seed")
            room.start_game()
            await broadcast_public_state(room)
            # Send initial private states (bottom 2 cards visible temporarily - handled on frontend)
            for p in room.players:
                key = (room_code, p.id)
                ws2 = player_sockets.get(key)
                if ws2:
                    await send_private_state(ws2, room, p.id)

        elif action == "draw_card":
            result = room.draw_card(player_id)
            await ws.send_text(json.dumps({"type": "action_result", "data": {"action": "draw_card", **result}}))
            await broadcast_public_state(room)

        elif action == "attempt_discard":
            position = int(payload["position"])
            result = room.attempt_discard(player_id, position)
            await ws.send_text(json.dumps({"type": "action_result", "data": {"action": "attempt_discard", **result}}))
            await send_private_state(ws, room, player_id)
            await broadcast_public_state(room)

        elif action == "keep_card":
            result = room.keep_card(player_id)
            await ws.send_text(json.dumps({"type": "action_result", "data": {"action": "keep_card", **result}}))
            await send_private_state(ws, room, player_id)
            await broadcast_public_state(room)

        elif action == "use_jack":
            target_id = payload["target_player_id"]
            position = int(payload["position"])
            result = room.use_jack(player_id, target_id, position)
            # Only reveal to the player who used jack
            await ws.send_text(json.dumps({"type": "action_result", "data": {"action": "use_jack", **result}}))
            await send_private_state(ws, room, player_id)
            await broadcast_public_state(room)

        elif action == "use_queen":
            result = room.use_queen(
                player_id,
                payload["player_a_id"], int(payload["pos_a"]),
                payload["player_b_id"], int(payload["pos_b"]),
            )
            await ws.send_text(json.dumps({"type": "action_result", "data": {"action": "use_queen", **result}}))
            # Update private state for both swapped players
            for pid in [payload["player_a_id"], payload["player_b_id"]]:
                await send_to_player(room, pid, {
                    "type": "private_state_update",
                    "data": room.private_state(pid),
                })
            await broadcast_public_state(room)

        elif action == "use_king_peek":
            target_id = payload["target_player_id"]
            position = int(payload["position"])
            result = room.use_king_peek(player_id, target_id, position)
            await ws.send_text(json.dumps({"type": "action_result", "data": {"action": "use_king_peek", **result}}))

        elif action == "use_king_swap":
            other_id = payload["other_player_id"]
            other_pos = int(payload["other_position"])
            result = room.use_king_swap(player_id, other_id, other_pos)
            await ws.send_text(json.dumps({"type": "action_result", "data": {"action": "use_king_swap", **result}}))
            for pid in set([player_id, other_id]):
                await send_to_player(room, pid, {
                    "type": "private_state_update",
                    "data": room.private_state(pid),
                })
            await broadcast_public_state(room)

        elif action == "call_rikiki":
            result = room.call_rikiki(player_id)
            await broadcast_public_state(room)
            await ws.send_text(json.dumps({"type": "action_result", "data": {"action": "call_rikiki", **result}}))

        else:
            await send_error(ws, f"Unknown action: {action}")

        # Check if game ended
        from models import GameState
        if room.state == GameState.ENDED:
            end_data = room.action_log[-1].details if room.action_log else {}
            for p in room.players:
                await send_to_player(room, p.id, {"type": "game_end", "data": end_data})

    except ValueError as e:
        await send_error(ws, str(e))
    except Exception as e:
        await send_error(ws, f"Server error: {str(e)}")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    room_code: Optional[str] = None
    player_id: Optional[str] = None

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            action = data.get("action")
            payload = data.get("payload", {})

            if action == "join_room":
                name = payload.get("name", "Player")
                code = payload.get("room_code", "").upper().strip()
                pid = payload.get("player_id", "")

                if not code:
                    # Create new room
                    code = generate_room_code()
                    while code in rooms:
                        code = generate_room_code()
                    rooms[code] = GameRoom(code, seed=payload.get("seed"))

                room = rooms.get(code)
                if not room:
                    await send_error(ws, "Room not found")
                    continue

                if not pid:
                    import uuid
                    pid = str(uuid.uuid4())[:8]

                try:
                    player = room.add_player(pid, name)
                except ValueError as e:
                    await send_error(ws, str(e))
                    continue

                room_code = code
                player_id = pid
                player_sockets[(code, pid)] = ws

                await ws.send_text(json.dumps({
                    "type": "joined",
                    "data": {
                        "room_code": code,
                        "player_id": pid,
                        "name": name,
                    }
                }))
                await broadcast_public_state(room)

                # Resync if game in progress
                from models import GameState
                if room.state != GameState.LOBBY:
                    await send_private_state(ws, room, pid)

            elif room_code and player_id:
                await handle_message(ws, data, room_code, player_id)
            else:
                await send_error(ws, "Not in a room. Send join_room first.")

    except WebSocketDisconnect:
        if room_code and player_id:
            room = rooms.get(room_code)
            if room:
                p = room.get_player(player_id)
                if p:
                    p.connected = False
            player_sockets.pop((room_code, player_id), None)
