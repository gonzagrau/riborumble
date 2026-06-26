"""
FastAPI app exposing Ribo-Rumble over HTTP + WebSocket.

State lives in process memory under a single asyncio.Lock — no DB,
no persistence. Restarting the server resets everything. For a LAN
party this is a feature, not a limitation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import game as gm
from codon import DEFAULT_CODON_TABLE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("riborumble")

app = FastAPI(title="Ribo-Rumble")

# --- Process-wide state ---------------------------------------------------

GAMES: dict[str, gm.Game] = {}
# Map (game_id, player_id) -> set of live WebSockets. A player may briefly
# have two sockets during a reconnect; broadcasting to all is safe.
SOCKETS: dict[tuple[str, str], set[WebSocket]] = {}
END_TASKS: dict[str, asyncio.Task] = {}
CLEANUP_TASKS: dict[str, asyncio.Task] = {}
LOCK = asyncio.Lock()
GAME_CLEANUP_AFTER_SECONDS = float(os.environ.get("GAME_CLEANUP_AFTER_SECONDS", "21600"))


# --- Request bodies -------------------------------------------------------


class TeamSpec(BaseModel):
    name: str = Field(min_length=1, max_length=gm.MAX_TEAM_NAME_LENGTH)
    color: str = Field(default="#888", pattern=gm.HEX_COLOR_PATTERN)


class CreateGameBody(BaseModel):
    host_name: str = Field(min_length=1, max_length=gm.MAX_PLAYER_NAME_LENGTH)
    expected_players: int = Field(ge=2, le=gm.MAX_EXPECTED_PLAYERS)
    game_duration_minutes: float = Field(default=15.0, gt=0)
    end_window_minutes: tuple[float, float] | None = None
    codon_table: dict[str, str] | None = Field(default=None, max_length=64)
    mode: str = "solo"  # "solo" or "teams"
    teams: list[TeamSpec] | None = None


class JoinGameBody(BaseModel):
    name: str = Field(min_length=1, max_length=gm.MAX_PLAYER_NAME_LENGTH)


# --- REST endpoints -------------------------------------------------------


@app.post("/api/games")
async def create_game(body: CreateGameBody) -> dict[str, Any]:
    async with LOCK:
        try:
            mode = gm.Mode(body.mode)
        except ValueError:
            raise HTTPException(400, f"Invalid mode: {body.mode}")
        try:
            duration_minutes = _duration_minutes_from_body(body)
            game, host = gm.new_game(
                host_name=body.host_name,
                expected_players=body.expected_players,
                game_duration_minutes=duration_minutes,
                codon_table=body.codon_table or dict(DEFAULT_CODON_TABLE),
                mode=mode,
                teams=[t.model_dump() for t in body.teams] if body.teams else None,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))
        GAMES[game.id] = game
        log.info(
            "Created game %s (host=%s, expect=%d, mode=%s, duration=%.1fm)",
            game.id, host.name, body.expected_players, mode.value, duration_minutes,
        )
        return {
            "game_id": game.id,
            "player_id": host.id,
            "host_player_id": host.id,
            "expected_players": game.expected_players,
            "game_duration_minutes": game.duration_seconds / 60,
            "codon_table": game.codon_table,
            "mode": game.mode.value,
            "teams": [gm._team_view(t) for t in game.teams.values()],
        }


@app.post("/api/games/{game_id}/join")
async def join_game(game_id: str, body: JoinGameBody) -> dict[str, Any]:
    async with LOCK:
        game = GAMES.get(game_id)
        if game is None:
            raise HTTPException(404, "Game not found")
        try:
            player = gm.join_game(game, body.name)
        except ValueError as e:
            raise HTTPException(400, str(e))
        log.info("Player %s joined game %s", player.name, game_id)
        # Notify lobby
        await _broadcast_lobby(game)
        return {
            "game_id": game.id,
            "player_id": player.id,
            "host_player_id": game.host_player_id,
            "expected_players": game.expected_players,
            "game_duration_minutes": game.duration_seconds / 60,
            "codon_table": game.codon_table,
            "mode": game.mode.value,
            "teams": [gm._team_view(t) for t in game.teams.values()],
        }


@app.get("/api/games/{game_id}/codon-table")
async def get_codon_table(game_id: str) -> dict[str, str]:
    game = GAMES.get(game_id)
    if game is None:
        raise HTTPException(404, "Game not found")
    return game.codon_table


# --- WebSocket ------------------------------------------------------------


def _duration_minutes_from_body(body: CreateGameBody) -> float:
    """Prefer the fixed duration field; accept exact legacy tuples only."""
    if "game_duration_minutes" in body.model_fields_set or body.end_window_minutes is None:
        return body.game_duration_minutes
    lo, hi = body.end_window_minutes
    if lo != hi:
        raise ValueError("Game duration must be a single fixed number of minutes")
    return lo


@app.websocket("/ws/{game_id}")
async def websocket_endpoint(ws: WebSocket, game_id: str, player_id: str) -> None:
    await ws.accept()
    game = GAMES.get(game_id)
    if game is None or player_id not in game.players:
        await ws.send_json({"type": "error", "message": "Unknown game or player"})
        await ws.close()
        return

    key = (game_id, player_id)
    SOCKETS.setdefault(key, set()).add(ws)
    game.players[player_id].connected = True
    log.info("WS connected: game=%s player=%s", game_id, game.players[player_id].name)

    # Immediately send the right snapshot for the current phase.
    if game.phase == gm.Phase.ENDED:
        await ws.send_json(gm.end_game(game))
    else:
        await ws.send_json(gm.state_snapshot_for(game, player_id))
        await _broadcast_lobby(game)

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "message": "Invalid JSON"})
                continue
            await _handle_client_message(game, player_id, msg, ws)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.exception("WS error for player %s: %s", player_id, e)
    finally:
        socks = SOCKETS.get(key, set())
        socks.discard(ws)
        if not socks:
            SOCKETS.pop(key, None)
            if player_id in game.players:
                game.players[player_id].connected = False
        log.info("WS disconnected: game=%s player=%s", game_id, player_id)
        if game.phase != gm.Phase.ENDED:
            await _broadcast_lobby(game)


# --- Message handlers -----------------------------------------------------


async def _handle_client_message(
    game: gm.Game, player_id: str, msg: dict, ws: WebSocket
) -> None:
    mtype = msg.get("type")
    post_actions: list[tuple[Any, ...]] = []
    try:
        if mtype == "ping":
            await ws.send_json({"type": "pong"})
            return

        async with LOCK:
            if mtype == "start_game":
                end_at = gm.start_game(game, player_id)
                task = asyncio.create_task(_run_ender(game.id, end_at))
                END_TASKS[game.id] = task
                log.info(
                    "Game %s started, will end at +%.1fs",
                    game.id,
                    end_at - (game.started_at or 0),
                )
                post_actions.append(("broadcast", {"type": "game_started"}))
                post_actions.append(("snapshots", None))

            elif mtype == "assign_team":
                gm.assign_team(game, player_id, msg.get("team_id"))
                post_actions.append(("lobby", None))

            elif mtype == "create_request":
                r = gm.create_request(
                    game,
                    requester_id=player_id,
                    protein_name=msg["protein_name"],
                    dna_sequence=msg["dna_sequence"],
                    decrypter_id=msg.get("decrypter_id"),
                    decrypter_team_id=msg.get("decrypter_team_id"),
                )
                post_actions.append(
                    (
                        "requester_side",
                        r,
                        {"type": "outgoing_update", "request": gm._request_view_for_requester(r)},
                    )
                )
                post_actions.append(
                    (
                        "decrypter_side",
                        r,
                        {"type": "incoming_update", "request": gm._request_view_for_decrypter(r)},
                    )
                )

            elif mtype == "submit_decryption":
                r = gm.submit_decryption(
                    game,
                    decrypter_id=player_id,
                    request_id=msg["request_id"],
                    peptide_guess=msg["peptide_guess"],
                )
                post_actions.append(
                    (
                        "requester_side",
                        r,
                        {"type": "outgoing_update", "request": gm._request_view_for_requester(r)},
                    )
                )
                post_actions.append(
                    (
                        "decrypter_side",
                        r,
                        {"type": "incoming_update", "request": gm._request_view_for_decrypter(r)},
                    )
                )

            elif mtype == "confirm_decryption":
                r, _ = gm.confirm_decryption(game, player_id, msg["request_id"])
                decision_was_correct = r.is_dna_valid and bool(r.submitted_peptide_is_correct)
                post_actions.append(("decision", r, "confirmed"))
                post_actions.append(
                    ("sound", [player_id], "correct" if decision_was_correct else "wrong")
                )
                post_actions.append(("sound", _request_decrypter_actor_ids(r), "correct"))
                post_actions.append(("scores", None))

            elif mtype == "reject_decryption":
                r, _ = gm.reject_decryption(game, player_id, msg["request_id"])
                decision_was_correct = r.is_dna_valid and not bool(r.submitted_peptide_is_correct)
                post_actions.append(("decision", r, "rejected"))
                post_actions.append(
                    ("sound", [player_id], "correct" if decision_was_correct else "wrong")
                )
                post_actions.append(("scores", None))

            elif mtype == "flag_invalid_request":
                r, _ = gm.flag_invalid_request(game, player_id, msg["request_id"])
                flag_was_correct = not r.is_dna_valid
                post_actions.append(("invalid_flag", r))
                post_actions.append(
                    ("sound", [player_id], "correct" if flag_was_correct else "wrong")
                )
                if flag_was_correct:
                    post_actions.append(("sound", [r.requester_id], "wrong"))
                post_actions.append(("scores", None))

            else:
                post_actions.append(("direct_error", f"Unknown message type: {mtype}"))
    except (ValueError, PermissionError, KeyError) as e:
        log.warning("Client error from %s on %s: %s", player_id, mtype, e)
        await ws.send_json({"type": "error", "message": str(e)})
        return

    for action in post_actions:
        kind = action[0]
        if kind == "broadcast":
            await _broadcast(game, action[1])
        elif kind == "snapshots":
            await _broadcast_snapshots(game)
        elif kind == "lobby":
            await _broadcast_lobby(game)
        elif kind == "requester_side":
            _, request, payload = action
            await _broadcast_to_requester_side(game, request, payload)
        elif kind == "decrypter_side":
            _, request, payload = action
            await _broadcast_to_decrypter_side(game, request, payload)
        elif kind == "decision":
            _, request, decision = action
            await _broadcast_decision(game, request, decision)
        elif kind == "invalid_flag":
            await _broadcast_invalid_flag(game, action[1])
        elif kind == "scores":
            await _broadcast_scores(game)
        elif kind == "sound":
            _, player_ids, sound = action
            await _broadcast_sound(game, player_ids, sound)
        elif kind == "direct_error":
            await ws.send_json({"type": "error", "message": action[1]})


# --- Broadcast helpers ----------------------------------------------------


async def _send_to_socket(ws: WebSocket, payload: dict) -> None:
    try:
        await ws.send_json(payload)
    except Exception:
        # Caller is fine with silent failure; the socket loop will clean up
        pass


async def _broadcast(game: gm.Game, payload: dict) -> None:
    for player_id in game.players:
        await _broadcast_to(game, player_id, payload)


async def _broadcast_to(game: gm.Game, player_id: str, payload: dict) -> None:
    for ws in list(SOCKETS.get((game.id, player_id), ())):
        await _send_to_socket(ws, payload)


async def _broadcast_sound(game: gm.Game, player_ids: list[str], sound: str) -> None:
    """Send a private sound cue to the listed players."""
    payload = {"type": "sound_effect", "sound": sound}
    for player_id in dict.fromkeys(pid for pid in player_ids if pid in game.players):
        await _broadcast_to(game, player_id, payload)


def _request_decrypter_actor_ids(r: gm.Request) -> list[str]:
    """The player who actually submitted, falling back to the solo assignee."""
    if r.submitted_by_id is not None:
        return [r.submitted_by_id]
    if r.decrypter_id is not None:
        return [r.decrypter_id]
    return []


def _team_member_ids(game: gm.Game, team_id: str | None) -> list[str]:
    """All player ids on a given team. Empty list if team_id is None or unknown."""
    if team_id is None:
        return []
    return [p.id for p in game.players.values() if p.team_id == team_id]


async def _broadcast_to_requester_side(game: gm.Game, r: gm.Request, payload: dict) -> None:
    """In solo mode, send to the requester; in team mode, fan out to the whole requester team."""
    if game.mode == gm.Mode.TEAMS:
        for pid in _team_member_ids(game, r.requester_team_id):
            await _broadcast_to(game, pid, payload)
    else:
        await _broadcast_to(game, r.requester_id, payload)


async def _broadcast_to_decrypter_side(game: gm.Game, r: gm.Request, payload: dict) -> None:
    """In solo mode, send to the decrypter; in team mode, fan out to the whole decrypter team."""
    if game.mode == gm.Mode.TEAMS:
        for pid in _team_member_ids(game, r.decrypter_team_id):
            await _broadcast_to(game, pid, payload)
    elif r.decrypter_id is not None:
        await _broadcast_to(game, r.decrypter_id, payload)


async def _broadcast_lobby(game: gm.Game) -> None:
    payload = {
        "type": "lobby_update",
        "phase": game.phase.value,
        "mode": game.mode.value,
        "players": [gm._player_view(p) for p in game.players.values()],
        "teams": [gm._team_view(t) for t in game.teams.values()],
        "expected_players": game.expected_players,
        "host_player_id": game.host_player_id,
    }
    await _broadcast(game, payload)


async def _broadcast_snapshots(game: gm.Game) -> None:
    for player_id in game.players:
        snap = gm.state_snapshot_for(game, player_id)
        await _broadcast_to(game, player_id, snap)


async def _broadcast_scores(game: gm.Game) -> None:
    """Broadcast only each recipient's own score/team score."""
    for player_id in game.players:
        await _broadcast_to(
            game,
            player_id,
            {"type": "score_update", "score": gm.score_view_for(game, player_id)},
        )


async def _broadcast_decision(game: gm.Game, r: gm.Request, decision: str) -> None:
    """Notify both sides of a confirm/reject."""
    await _broadcast_to_requester_side(
        game, r,
        {"type": "outgoing_update", "request": gm._request_view_for_requester(r)},
    )
    await _broadcast_to_decrypter_side(
        game, r,
        {
            "type": "decision_made",
            "request": gm._request_view_for_decrypter(r),
            "decision": decision,
        },
    )


async def _broadcast_invalid_flag(game: gm.Game, r: gm.Request) -> None:
    """Notify both sides when a receiver claims invalid DNA."""
    await _broadcast_to_requester_side(
        game, r,
        {"type": "outgoing_update", "request": gm._request_view_for_requester(r)},
    )
    await _broadcast_to_decrypter_side(
        game, r,
        {"type": "invalid_flagged", "request": gm._request_view_for_decrypter(r)},
    )


# --- Scheduled game end ---------------------------------------------------


async def _run_ender(game_id: str, end_at: float) -> None:
    """Sleep until end_at, then transition the game to ENDED."""
    import time
    delay = max(0.0, end_at - time.time())
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    async with LOCK:
        game = GAMES.get(game_id)
        if game is None or game.phase == gm.Phase.ENDED:
            return
        reveal = gm.end_game(game)
        log.info("Game %s ended", game_id)
    # Broadcast outside the lock — sending JSON over sockets shouldn't
    # block other state mutations
    await _broadcast(game, reveal)
    if GAME_CLEANUP_AFTER_SECONDS >= 0:
        CLEANUP_TASKS[game_id] = asyncio.create_task(
            _cleanup_game_later(game_id, GAME_CLEANUP_AFTER_SECONDS)
        )


async def _cleanup_game_later(game_id: str, delay: float) -> None:
    """Remove ended games after a grace period so final reveals remain reconnectable."""
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    async with LOCK:
        game = GAMES.get(game_id)
        if game is None or game.phase != gm.Phase.ENDED:
            return
        GAMES.pop(game_id, None)
        END_TASKS.pop(game_id, None)
        CLEANUP_TASKS.pop(game_id, None)
        for key in [key for key in SOCKETS if key[0] == game_id]:
            SOCKETS.pop(key, None)
        log.info("Cleaned up ended game %s", game_id)


# --- Static frontend ------------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

SOUNDS_DIR = Path(__file__).parent / "sounds"
if SOUNDS_DIR.exists():
    app.mount("/sounds", StaticFiles(directory=str(SOUNDS_DIR)), name="sounds")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/favicon.ico", include_in_schema=False)
@app.head("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "favicon.png"), media_type="image/png")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# --- Dev entry point ------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("main:app", host=host, port=port, reload=False, log_level="info")
