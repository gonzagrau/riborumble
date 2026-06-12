"""
Core game state and transitions.

This module is intentionally network-free: no FastAPI, no WebSockets.
The web layer in main.py calls into these functions and broadcasts the
returned events. That separation makes the state machine straightforward
to reason about and test.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from codon import (
    DEFAULT_CODON_TABLE,
    is_dna_valid,
    peptides_match,
    translate_dna,
)


class Phase(str, Enum):
    LOBBY = "lobby"
    IN_PROGRESS = "in_progress"
    ENDED = "ended"


class RequestState(str, Enum):
    AWAITING_DECRYPTION = "awaiting_decryption"
    PENDING_APPROVAL = "pending_approval"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class Mode(str, Enum):
    SOLO = "solo"
    TEAMS = "teams"


# --- Scoring constants ----------------------------------------------------

POINTS_CORRECT_CONFIRMED = +3   # decrypter rewarded for confirmed answer
POINTS_REJECTED = -3            # decrypter penalized when requester rejects
POINTS_INVALID_DNA_REJECTED = -3  # requester penalized when caught sending garbage
POINTS_REQUESTER_BAD_DECISION = -2  # requester penalized for confirm-wrong or reject-correct
POINTS_END_AWAITING = -2        # decrypter penalized for undecrypted request at game end
POINTS_END_PENDING = +2         # decrypter rewarded for unresolved submission at game end


# --- Data classes ---------------------------------------------------------


@dataclass
class Team:
    id: str
    name: str
    color: str           # hex color string, e.g. "#f85149"
    score: int = 0


@dataclass
class Player:
    id: str
    name: str
    score: int = 0
    connected: bool = False
    team_id: str | None = None   # set only in team mode


@dataclass
class Request:
    id: str
    requester_id: str            # initiator (always an individual)
    decrypter_id: str | None     # set in SOLO mode; None in TEAMS mode
    decrypter_team_id: str | None  # set in TEAMS mode; None in SOLO mode
    requester_team_id: str | None  # set in TEAMS mode for routing; None in SOLO mode
    protein_name: str
    dna_sequence: str            # truth, never sent to decrypter
    correct_peptide: str         # canonical form, never sent to anyone during play
    is_dna_valid: bool           # internal flag, never sent to anyone during play
    state: RequestState = RequestState.AWAITING_DECRYPTION
    submitted_peptide: str | None = None
    submitted_peptide_is_correct: bool | None = None
    submitted_by_id: str | None = None   # which teammate actually decrypted (audit)
    resolved_by_id: str | None = None    # which teammate actually confirmed/rejected (audit)
    created_at: float = field(default_factory=time.time)
    decrypted_at: float | None = None
    resolved_at: float | None = None


@dataclass
class Game:
    id: str
    host_player_id: str
    expected_players: int
    end_window_seconds: tuple[float, float]
    codon_table: dict[str, str]
    mode: Mode = Mode.SOLO
    phase: Phase = Phase.LOBBY
    players: dict[str, Player] = field(default_factory=dict)
    teams: dict[str, Team] = field(default_factory=dict)
    requests: dict[str, Request] = field(default_factory=dict)
    started_at: float | None = None
    scheduled_end_at: float | None = None
    ended_at: float | None = None


# --- Event payloads -------------------------------------------------------
# Returned by state transitions so the web layer knows what to broadcast
# and to whom. Events are plain dicts to keep JSON serialization trivial.

Event = dict[str, Any]


def _player_view(p: Player) -> Event:
    return {
        "id": p.id,
        "name": p.name,
        "connected": p.connected,
        "team_id": p.team_id,
    }


def _team_view(t: Team) -> Event:
    return {"id": t.id, "name": t.name, "color": t.color}


def _request_view_for_requester(r: Request) -> Event:
    """What a requester (or their teammate in team mode) sees about an outgoing request."""
    return {
        "id": r.id,
        "role": "requester",
        "requester_id": r.requester_id,
        "decrypter_id": r.decrypter_id,            # None in team mode
        "decrypter_team_id": r.decrypter_team_id,  # None in solo mode
        "protein_name": r.protein_name,
        "dna_sequence": r.dna_sequence,            # requester side sees DNA
        "state": r.state.value,
        "submitted_peptide": r.submitted_peptide,
        "submitted_by_id": r.submitted_by_id,      # audit: which teammate decrypted
        "resolved_by_id": r.resolved_by_id,        # audit: which teammate decided
    }


def _request_view_for_decrypter(r: Request) -> Event:
    """What a decrypter (or their teammate in team mode) sees — no truth."""
    return {
        "id": r.id,
        "role": "decrypter",
        "requester_id": r.requester_id,
        "requester_team_id": r.requester_team_id,  # None in solo mode
        "protein_name": r.protein_name,
        "dna_sequence": r.dna_sequence,
        "state": r.state.value,
        "submitted_peptide": r.submitted_peptide,
        "submitted_by_id": r.submitted_by_id,
        "resolved_by_id": r.resolved_by_id,
    }


def state_snapshot_for(game: Game, player_id: str) -> Event:
    """
    Full per-player state, used on connect/reconnect.

    In solo mode: returns requests where this player is requester or decrypter.
    In teams mode: returns requests where this player's TEAM is the requester
    team or decrypter team — i.e. shared inboxes/outboxes per team.
    """
    if game.mode == Mode.TEAMS:
        my_team = game.players[player_id].team_id if player_id in game.players else None
        outgoing = [
            _request_view_for_requester(r)
            for r in game.requests.values()
            if r.requester_team_id == my_team
        ]
        incoming = [
            _request_view_for_decrypter(r)
            for r in game.requests.values()
            if r.decrypter_team_id == my_team
        ]
    else:
        outgoing = [
            _request_view_for_requester(r)
            for r in game.requests.values()
            if r.requester_id == player_id
        ]
        incoming = [
            _request_view_for_decrypter(r)
            for r in game.requests.values()
            if r.decrypter_id == player_id
        ]
    return {
        "type": "state_sync",
        "phase": game.phase.value,
        "you": player_id,
        "mode": game.mode.value,
        "players": [_player_view(p) for p in game.players.values()],
        "teams": [_team_view(t) for t in game.teams.values()],
        "expected_players": game.expected_players,
        "host_player_id": game.host_player_id,
        "codon_table": game.codon_table,
        "outgoing": outgoing,
        "incoming": incoming,
    }


# --- Helpers --------------------------------------------------------------


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(6)}"


def new_game(
    host_name: str,
    expected_players: int,
    end_window_minutes: tuple[float, float],
    codon_table: dict[str, str] | None = None,
    mode: Mode = Mode.SOLO,
    teams: list[dict[str, str]] | None = None,
) -> tuple[Game, Player]:
    """
    Create a new game in LOBBY phase, with the host as the first player.

    In team mode, `teams` is a list of {"name", "color"} dicts (at least 2).
    The host is created without a team assignment — they pick from the
    lobby dropdown like everyone else.
    """
    if expected_players < 2:
        raise ValueError("Need at least 2 players")
    lo, hi = end_window_minutes
    if not (0 < lo <= hi):
        raise ValueError("Invalid end window")
    host = Player(id=_new_id("p"), name=host_name.strip())
    if not host.name:
        raise ValueError("Host name required")

    game = Game(
        id=_new_id("g"),
        host_player_id=host.id,
        expected_players=expected_players,
        end_window_seconds=(lo * 60, hi * 60),
        codon_table=codon_table or dict(DEFAULT_CODON_TABLE),
        mode=mode,
    )

    if mode == Mode.TEAMS:
        if not teams or len(teams) < 2:
            raise ValueError("Team mode needs at least 2 teams")
        if expected_players < len(teams):
            raise ValueError("Not enough players for that many teams")
        for spec in teams:
            tname = spec.get("name", "").strip()
            tcolor = spec.get("color", "#888")
            if not tname:
                raise ValueError("Each team needs a name")
            t = Team(id=_new_id("t"), name=tname, color=tcolor)
            game.teams[t.id] = t

    game.players[host.id] = host
    return game, host


def join_game(game: Game, name: str) -> Player:
    """
    Add a player to the lobby, or return the existing player if their
    name (case-insensitive) is already on the roster. Rejoin-by-name
    works in any phase; capacity check only applies to fresh joins
    during LOBBY.
    """
    name = name.strip()
    if not name:
        raise ValueError("Name required")
    existing = next(
        (p for p in game.players.values() if p.name.lower() == name.lower()),
        None,
    )
    if existing is not None:
        return existing
    if game.phase != Phase.LOBBY:
        raise ValueError("Game already started; you can only rejoin under your existing name")
    if len(game.players) >= game.expected_players:
        raise ValueError("Game is full")
    p = Player(id=_new_id("p"), name=name)
    game.players[p.id] = p
    return p


def assign_team(game: Game, player_id: str, team_id: str | None) -> Player:
    """Set a player's team. Only meaningful in team mode; only allowed in lobby."""
    if game.mode != Mode.TEAMS:
        raise ValueError("Not a team-mode game")
    if game.phase != Phase.LOBBY:
        raise ValueError("Teams can only be picked in the lobby")
    if player_id not in game.players:
        raise ValueError("Unknown player")
    if team_id is not None and team_id not in game.teams:
        raise ValueError("Unknown team")
    game.players[player_id].team_id = team_id
    return game.players[player_id]


def start_game(game: Game, by_player_id: str) -> float:
    """
    Host transitions the game to IN_PROGRESS and a random end time is
    chosen within the configured window. Returns the absolute end
    timestamp so the web layer can schedule the ender.
    """
    if by_player_id != game.host_player_id:
        raise PermissionError("Only the host can start the game")
    if game.phase != Phase.LOBBY:
        raise ValueError("Game already started")
    if len(game.players) < game.expected_players:
        raise ValueError(
            f"Waiting for players ({len(game.players)}/{game.expected_players})"
        )
    if game.mode == Mode.TEAMS:
        # Every player must be on a team, and every team must have at least one player.
        unassigned = [p.name for p in game.players.values() if p.team_id is None]
        if unassigned:
            raise ValueError(f"Players without team: {', '.join(unassigned)}")
        per_team = {tid: 0 for tid in game.teams}
        for p in game.players.values():
            per_team[p.team_id] = per_team.get(p.team_id, 0) + 1
        empty = [game.teams[tid].name for tid, n in per_team.items() if n == 0]
        if empty:
            raise ValueError(f"Empty teams: {', '.join(empty)}")

    lo, hi = game.end_window_seconds
    duration = secrets.SystemRandom().uniform(lo, hi)
    now = time.time()
    game.phase = Phase.IN_PROGRESS
    game.started_at = now
    game.scheduled_end_at = now + duration
    return game.scheduled_end_at


# --- Player actions -------------------------------------------------------


def _credit(game: Game, player_id: str, delta: int) -> tuple[str, int]:
    """
    Apply a score delta. In team mode the points go to the player's team
    and the returned tuple's first element is the team id (so the web
    layer can broadcast team scoreboards). In solo mode the points go to
    the player.
    """
    if game.mode == Mode.TEAMS:
        team_id = game.players[player_id].team_id
        if team_id is None:
            # Should be unreachable after start_game's checks
            raise RuntimeError(f"Player {player_id} has no team in team mode")
        game.teams[team_id].score += delta
        return (team_id, delta)
    else:
        game.players[player_id].score += delta
        return (player_id, delta)


def create_request(
    game: Game,
    requester_id: str,
    protein_name: str,
    dna_sequence: str,
    decrypter_id: str | None = None,
    decrypter_team_id: str | None = None,
) -> Request:
    """
    Log a new outgoing request.

    Assignment is random and always excludes the requester (solo mode) or
    the requester's own team (team mode). Optional decrypter arguments are
    accepted for backward compatibility but ignored.
    """
    if game.phase != Phase.IN_PROGRESS:
        raise ValueError("Game is not in progress")
    if requester_id not in game.players:
        raise ValueError("Unknown requester")
    protein_name = protein_name.strip()
    if not protein_name:
        raise ValueError("Protein name required")
    dna_sequence = dna_sequence.strip()
    if not dna_sequence:
        raise ValueError("DNA sequence required")

    requester = game.players[requester_id]
    truth = translate_dna(dna_sequence, game.codon_table)
    valid = is_dna_valid(dna_sequence, game.codon_table)

    if game.mode == Mode.TEAMS:
        if requester.team_id is None:
            raise ValueError("Requester must be assigned to a team")
        eligible_team_ids = [tid for tid in game.teams if tid != requester.team_id]
        if not eligible_team_ids:
            raise ValueError("No opposing team available")
        assigned_team_id = secrets.choice(eligible_team_ids)
        r = Request(
            id=_new_id("r"),
            requester_id=requester_id,
            decrypter_id=None,
            decrypter_team_id=assigned_team_id,
            requester_team_id=requester.team_id,
            protein_name=protein_name,
            dna_sequence=dna_sequence,
            correct_peptide=truth,
            is_dna_valid=valid,
        )
    else:
        eligible_player_ids = [pid for pid in game.players if pid != requester_id]
        if not eligible_player_ids:
            raise ValueError("No other player available")
        assigned_player_id = secrets.choice(eligible_player_ids)
        r = Request(
            id=_new_id("r"),
            requester_id=requester_id,
            decrypter_id=assigned_player_id,
            decrypter_team_id=None,
            requester_team_id=None,
            protein_name=protein_name,
            dna_sequence=dna_sequence,
            correct_peptide=truth,
            is_dna_valid=valid,
        )

    game.requests[r.id] = r
    return r


def submit_decryption(
    game: Game,
    decrypter_id: str,
    request_id: str,
    peptide_guess: str,
) -> Request:
    """
    Log a decryption guess.

    In SOLO mode the caller must be the designated decrypter.
    In TEAMS mode any teammate of the decrypter team can submit.
    First-write-wins: a second submitter sees "not awaiting decryption".
    """
    if game.phase != Phase.IN_PROGRESS:
        raise ValueError("Game is not in progress")
    r = game.requests.get(request_id)
    if r is None:
        raise ValueError("Unknown request")
    _authorize_decrypter(game, r, decrypter_id)
    if r.state != RequestState.AWAITING_DECRYPTION:
        raise ValueError("Request is not awaiting decryption")
    peptide_guess = peptide_guess.strip()
    if not peptide_guess:
        raise ValueError("Empty peptide guess")
    r.submitted_peptide = peptide_guess
    r.submitted_peptide_is_correct = peptides_match(peptide_guess, r.correct_peptide)
    r.submitted_by_id = decrypter_id
    r.state = RequestState.PENDING_APPROVAL
    r.decrypted_at = time.time()
    return r


def confirm_decryption(
    game: Game, requester_id: str, request_id: str
) -> tuple[Request, list[tuple[str, int]]]:
    """
    Requester (or teammate in team mode) accepts a decryption.

    Scoring:
      - whoever decrypted always gets +3 (decision honored)
      - whoever requested gets -2 if their confirm was misguided
        (wrong submission, or invalid DNA they should've caught)
    """
    r = _resolve_pending(game, requester_id, request_id)
    r.state = RequestState.CONFIRMED
    r.resolved_at = time.time()
    r.resolved_by_id = requester_id
    deltas: list[tuple[str, int]] = []

    deltas.append(_credit(game, _decrypter_credit_anchor(game, r), POINTS_CORRECT_CONFIRMED))

    decision_was_correct = r.is_dna_valid and bool(r.submitted_peptide_is_correct)
    if not decision_was_correct:
        deltas.append(_credit(game, r.requester_id, POINTS_REQUESTER_BAD_DECISION))

    return r, deltas


def reject_decryption(
    game: Game, requester_id: str, request_id: str
) -> tuple[Request, list[tuple[str, int]]]:
    """
    Requester (or teammate in team mode) rejects a decryption.

    Scoring:
      - invalid DNA: requester -3, decrypter unaffected
      - valid DNA: decrypter -3
      - valid DNA + actually-correct submission: requester also -2
    """
    r = _resolve_pending(game, requester_id, request_id)
    r.state = RequestState.REJECTED
    r.resolved_at = time.time()
    r.resolved_by_id = requester_id
    deltas: list[tuple[str, int]] = []

    if not r.is_dna_valid:
        deltas.append(_credit(game, r.requester_id, POINTS_INVALID_DNA_REJECTED))
        return r, deltas

    deltas.append(_credit(game, _decrypter_credit_anchor(game, r), POINTS_REJECTED))
    if r.submitted_peptide_is_correct:
        deltas.append(_credit(game, r.requester_id, POINTS_REQUESTER_BAD_DECISION))

    return r, deltas


def _decrypter_credit_anchor(game: Game, r: Request) -> str:
    """
    Pick a player_id that _credit can use to route the score to the right
    individual (solo) or team (teams). In team mode we prefer the player
    who actually submitted; if for some reason that's not set, we fall
    back to any teammate of the decrypter team.
    """
    if game.mode == Mode.TEAMS:
        if r.submitted_by_id is not None:
            return r.submitted_by_id
        # Fall back: any player on the decrypter team
        for p in game.players.values():
            if p.team_id == r.decrypter_team_id:
                return p.id
        raise RuntimeError(f"No player on decrypter team for request {r.id}")
    else:
        assert r.decrypter_id is not None
        return r.decrypter_id


def _authorize_decrypter(game: Game, r: Request, player_id: str) -> None:
    """Raise PermissionError unless the player is allowed to decrypt this request."""
    if game.mode == Mode.TEAMS:
        actor = game.players.get(player_id)
        if actor is None or actor.team_id != r.decrypter_team_id:
            raise PermissionError("Not your team's request to decrypt")
    else:
        if r.decrypter_id != player_id:
            raise PermissionError("Not your request to decrypt")


def _resolve_pending(game: Game, requester_id: str, request_id: str) -> Request:
    """
    Auth check for confirm/reject: requester (solo) or any teammate of
    the requester team (team mode) may resolve a pending request.
    """
    if game.phase != Phase.IN_PROGRESS:
        raise ValueError("Game is not in progress")
    r = game.requests.get(request_id)
    if r is None:
        raise ValueError("Unknown request")
    if game.mode == Mode.TEAMS:
        actor = game.players.get(requester_id)
        if actor is None or actor.team_id != r.requester_team_id:
            raise PermissionError("Not your team's request to resolve")
    else:
        if r.requester_id != requester_id:
            raise PermissionError("Not your request to resolve")
    if r.state != RequestState.PENDING_APPROVAL:
        raise ValueError("Request is not pending approval")
    return r


# --- Game end -------------------------------------------------------------


def end_game(game: Game) -> Event:
    """
    Transition to ENDED, apply sweep scoring for unresolved requests,
    and return the final reveal payload.

    Sweep rules:
      - AWAITING_DECRYPTION  -> decrypter -2  (request left undecrypted)
      - PENDING_APPROVAL     -> decrypter +2  (submission left unjudged)
    """
    if game.phase == Phase.ENDED:
        # Idempotent: still return the same reveal so a late client gets it
        return _final_reveal(game)
    game.phase = Phase.ENDED
    game.ended_at = time.time()

    sweep: list[dict[str, Any]] = []
    for r in game.requests.values():
        if r.state == RequestState.AWAITING_DECRYPTION:
            # No one decrypted, so for team mode we charge the team via any teammate.
            anchor = _decrypter_credit_anchor(game, r) if game.mode == Mode.SOLO \
                else _any_team_member_id(game, r.decrypter_team_id)
            credited_id, delta = _credit(game, anchor, POINTS_END_AWAITING)
            sweep.append({
                "request_id": r.id,
                "credited_id": credited_id,  # player_id or team_id
                "delta": delta,
                "reason": "left_undecrypted",
            })
        elif r.state == RequestState.PENDING_APPROVAL:
            anchor = _decrypter_credit_anchor(game, r)
            credited_id, delta = _credit(game, anchor, POINTS_END_PENDING)
            sweep.append({
                "request_id": r.id,
                "credited_id": credited_id,
                "delta": delta,
                "reason": "left_unresolved",
            })

    reveal = _final_reveal(game)
    reveal["sweep"] = sweep
    return reveal


def _any_team_member_id(game: Game, team_id: str | None) -> str:
    """Return any player_id belonging to the given team. Raises if no member exists."""
    for p in game.players.values():
        if p.team_id == team_id:
            return p.id
    raise RuntimeError(f"No member found for team {team_id}")


def _final_reveal(game: Game) -> Event:
    """End-of-game payload with full truth: scores, all requests, all answers."""
    player_scores = sorted(
        ({"id": p.id, "name": p.name, "score": p.score, "team_id": p.team_id}
         for p in game.players.values()),
        key=lambda x: x["score"],
        reverse=True,
    )
    team_scores = sorted(
        ({"id": t.id, "name": t.name, "color": t.color, "score": t.score}
         for t in game.teams.values()),
        key=lambda x: x["score"],
        reverse=True,
    )
    requests = []
    for r in game.requests.values():
        requests.append({
            "id": r.id,
            "requester_id": r.requester_id,
            "requester_team_id": r.requester_team_id,
            "decrypter_id": r.decrypter_id,
            "decrypter_team_id": r.decrypter_team_id,
            "protein_name": r.protein_name,
            "dna_sequence": r.dna_sequence,
            "correct_peptide": r.correct_peptide,
            "is_dna_valid": r.is_dna_valid,
            "submitted_peptide": r.submitted_peptide,
            "submitted_peptide_is_correct": r.submitted_peptide_is_correct,
            "submitted_by_id": r.submitted_by_id,
            "resolved_by_id": r.resolved_by_id,
            "state": r.state.value,
        })
    return {
        "type": "game_ended",
        "mode": game.mode.value,
        "scoreboard": player_scores,    # individual scores (used in solo mode)
        "team_scoreboard": team_scores, # team scores (used in teams mode)
        "requests": requests,
    }
