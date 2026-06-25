"""
Core game state and transitions.

This module is intentionally network-free: no FastAPI, no WebSockets.
The web layer in main.py calls into these functions and broadcasts the
returned events. That separation makes the state machine straightforward
to reason about and test.
"""

from __future__ import annotations

import secrets
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from codon import (
    DEFAULT_CODON_TABLE,
    is_dna_valid,
    normalize_codon_table,
    normalize_dna,
    peptides_match,
    sequence_amino_acid_length,
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
    FLAGGED_INVALID = "flagged_invalid"


class Mode(str, Enum):
    SOLO = "solo"
    TEAMS = "teams"


# --- Scoring constants ----------------------------------------------------

POINTS_CORRECT_CONFIRMED = +3   # decrypter rewarded for confirmed answer
POINTS_REQUESTER_VALID_TRANSLATION = +1  # requester rewarded for valid confirmed translation
POINTS_INVALID_DNA_CAUGHT = +1   # decrypter rewarded for catching invalid DNA
POINTS_REJECTED = -3            # decrypter penalized when requester rejects
POINTS_FALSE_INVALID_FLAG = -3   # decrypter penalized for wrongly flagging valid DNA
POINTS_INVALID_DNA_REJECTED = -3  # requester penalized when caught sending garbage
POINTS_REQUESTER_BAD_DECISION = -2  # requester penalized for confirm-wrong or reject-correct
POINTS_END_AWAITING = -2        # decrypter penalized for undecrypted request at game end
POINTS_END_PENDING = +2         # decrypter rewarded for unresolved submission at game end

MAX_PLAYER_NAME_LENGTH = 40
MAX_TEAM_NAME_LENGTH = 32
MAX_PROTEIN_NAME_LENGTH = 60
MAX_DNA_LENGTH = 300
MAX_PEPTIDE_LENGTH = 400
HEX_COLOR_PATTERN = r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$"
_HEX_COLOR_RE = re.compile(HEX_COLOR_PATTERN)


# --- Data classes ---------------------------------------------------------


@dataclass
class Team:
    id: str
    name: str
    color: str           # hex color string, e.g. "#f85149"
    score: float = 0.0


@dataclass
class Player:
    id: str
    name: str
    score: float = 0.0
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
    dna_sequence: str            # DNA shown to both sides; validity/truth stay hidden
    correct_peptide: str         # canonical form, never sent to anyone during play
    is_dna_valid: bool           # internal flag, never sent to anyone during play
    sequence_aa_length: int
    point_multiplier: float
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


def score_view_for(game: Game, player_id: str) -> Event:
    """Private score payload for one player: own score in solo, own team score in teams."""
    player = game.players[player_id]
    if game.mode == Mode.TEAMS:
        if player.team_id is None:
            return {"scope": "team", "id": None, "name": None, "score": 0.0}
        team = game.teams[player.team_id]
        return {"scope": "team", "id": team.id, "name": team.name, "score": team.score}
    return {"scope": "player", "id": player.id, "name": player.name, "score": player.score}


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
        "sequence_aa_length": r.sequence_aa_length,
        "point_multiplier": r.point_multiplier,
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
        "sequence_aa_length": r.sequence_aa_length,
        "point_multiplier": r.point_multiplier,
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
        "score": score_view_for(game, player_id),
        "expected_players": game.expected_players,
        "host_player_id": game.host_player_id,
        "codon_table": game.codon_table,
        "outgoing": outgoing,
        "incoming": incoming,
    }


# --- Helpers --------------------------------------------------------------


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(6)}"


def _clean_text(value: str, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} required")
    if len(cleaned) > max_length:
        raise ValueError(f"{field_name} must be at most {max_length} characters")
    return cleaned


def _clean_dna(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("DNA sequence must be text")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("DNA sequence required")
    normalized_length = len(normalize_dna(cleaned))
    if normalized_length > MAX_DNA_LENGTH:
        raise ValueError(f"DNA sequence must be at most {MAX_DNA_LENGTH} bases")
    return cleaned


def _clean_team_color(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Team color must be text")
    cleaned = value.strip()
    if not _HEX_COLOR_RE.fullmatch(cleaned):
        raise ValueError("Team color must be a hex color like #58a6ff")
    return cleaned


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
    host = Player(
        id=_new_id("p"),
        name=_clean_text(host_name, "Host name", MAX_PLAYER_NAME_LENGTH),
    )

    game = Game(
        id=_new_id("g"),
        host_player_id=host.id,
        expected_players=expected_players,
        end_window_seconds=(lo * 60, hi * 60),
        codon_table=normalize_codon_table(codon_table or dict(DEFAULT_CODON_TABLE)),
        mode=mode,
    )

    if mode == Mode.TEAMS:
        if not teams or len(teams) < 2:
            raise ValueError("Team mode needs at least 2 teams")
        if expected_players < len(teams):
            raise ValueError("Not enough players for that many teams")
        for spec in teams:
            tname = _clean_text(
                spec.get("name", ""), "Team name", MAX_TEAM_NAME_LENGTH
            )
            tcolor = _clean_team_color(spec.get("color", "#888"))
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
    name = _clean_text(name, "Name", MAX_PLAYER_NAME_LENGTH)
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


def _point_multiplier(sequence_aa_length: int) -> float:
    return round(1 + sequence_aa_length / 10, 1)


def _scaled_delta(r: Request, base_delta: int) -> float:
    return round(base_delta * r.point_multiplier, 1)


def _credit(game: Game, player_id: str, delta: float) -> tuple[str, float]:
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


def _credit_scaled(
    game: Game, player_id: str, r: Request, base_delta: int
) -> tuple[str, float]:
    return _credit(game, player_id, _scaled_delta(r, base_delta))


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
    protein_name = _clean_text(protein_name, "Protein name", MAX_PROTEIN_NAME_LENGTH)
    dna_sequence = _clean_dna(dna_sequence)

    requester = game.players[requester_id]
    truth = translate_dna(dna_sequence, game.codon_table)
    valid = is_dna_valid(dna_sequence, game.codon_table)
    aa_length = sequence_amino_acid_length(dna_sequence)
    point_multiplier = _point_multiplier(aa_length)

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
            sequence_aa_length=aa_length,
            point_multiplier=point_multiplier,
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
            sequence_aa_length=aa_length,
            point_multiplier=point_multiplier,
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
    peptide_guess = _clean_text(peptide_guess, "Peptide guess", MAX_PEPTIDE_LENGTH)
    r.submitted_peptide = peptide_guess
    r.submitted_peptide_is_correct = peptides_match(peptide_guess, r.correct_peptide)
    r.submitted_by_id = decrypter_id
    r.state = RequestState.PENDING_APPROVAL
    r.decrypted_at = time.time()
    return r


def flag_invalid_request(
    game: Game,
    decrypter_id: str,
    request_id: str,
) -> tuple[Request, list[tuple[str, float]]]:
    """
    Receiver claims the DNA itself is invalid.

    Correct flags close the request, reward the receiver, and penalize the
    sender. False flags close the request too, but penalize the receiver.
    """
    if game.phase != Phase.IN_PROGRESS:
        raise ValueError("Game is not in progress")
    r = game.requests.get(request_id)
    if r is None:
        raise ValueError("Unknown request")
    _authorize_decrypter(game, r, decrypter_id)
    if r.state != RequestState.AWAITING_DECRYPTION:
        raise ValueError("Request is not awaiting decryption")

    r.submitted_peptide = "Invalid DNA"
    r.submitted_peptide_is_correct = not r.is_dna_valid
    r.submitted_by_id = decrypter_id
    r.state = RequestState.FLAGGED_INVALID
    r.decrypted_at = time.time()
    r.resolved_at = r.decrypted_at
    r.resolved_by_id = decrypter_id

    deltas: list[tuple[str, float]] = []
    if r.is_dna_valid:
        deltas.append(
            _credit_scaled(game, decrypter_id, r, POINTS_FALSE_INVALID_FLAG)
        )
    else:
        deltas.append(
            _credit_scaled(game, decrypter_id, r, POINTS_INVALID_DNA_CAUGHT)
        )
        deltas.append(
            _credit_scaled(game, r.requester_id, r, POINTS_INVALID_DNA_REJECTED)
        )
    return r, deltas


def confirm_decryption(
    game: Game, requester_id: str, request_id: str
) -> tuple[Request, list[tuple[str, float]]]:
    """
    Requester (or teammate in team mode) accepts a decryption.

    Scoring:
      - whoever decrypted always gets +3m (decision honored)
      - whoever requested gets +1m for confirming a valid correct translation
      - whoever requested gets -2m if their confirm was misguided
        (wrong submission, or invalid DNA they should've caught)
    """
    r = _resolve_pending(game, requester_id, request_id)
    r.state = RequestState.CONFIRMED
    r.resolved_at = time.time()
    r.resolved_by_id = requester_id
    deltas: list[tuple[str, float]] = []

    deltas.append(
        _credit_scaled(game, _decrypter_credit_anchor(game, r), r, POINTS_CORRECT_CONFIRMED)
    )

    decision_was_correct = r.is_dna_valid and bool(r.submitted_peptide_is_correct)
    if decision_was_correct:
        deltas.append(
            _credit_scaled(game, r.requester_id, r, POINTS_REQUESTER_VALID_TRANSLATION)
        )
    else:
        deltas.append(
            _credit_scaled(game, r.requester_id, r, POINTS_REQUESTER_BAD_DECISION)
        )

    return r, deltas


def reject_decryption(
    game: Game, requester_id: str, request_id: str
) -> tuple[Request, list[tuple[str, float]]]:
    """
    Requester (or teammate in team mode) rejects a decryption.

    Scoring:
      - invalid DNA: requester -3m, decrypter +1m
      - valid DNA: decrypter -3m
      - valid DNA + actually-correct submission: requester also -2m
    """
    r = _resolve_pending(game, requester_id, request_id)
    r.state = RequestState.REJECTED
    r.resolved_at = time.time()
    r.resolved_by_id = requester_id
    deltas: list[tuple[str, float]] = []

    if not r.is_dna_valid:
        deltas.append(
            _credit_scaled(game, r.requester_id, r, POINTS_INVALID_DNA_REJECTED)
        )
        deltas.append(
            _credit_scaled(game, _decrypter_credit_anchor(game, r), r, POINTS_INVALID_DNA_CAUGHT)
        )
        return r, deltas

    deltas.append(
        _credit_scaled(game, _decrypter_credit_anchor(game, r), r, POINTS_REJECTED)
    )
    if r.submitted_peptide_is_correct:
        deltas.append(
            _credit_scaled(game, r.requester_id, r, POINTS_REQUESTER_BAD_DECISION)
        )

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
            credited_id, delta = _credit_scaled(game, anchor, r, POINTS_END_AWAITING)
            sweep.append({
                "request_id": r.id,
                "credited_id": credited_id,  # player_id or team_id
                "delta": delta,
                "reason": "left_undecrypted",
            })
        elif r.state == RequestState.PENDING_APPROVAL:
            anchor = _decrypter_credit_anchor(game, r)
            credited_id, delta = _credit_scaled(game, anchor, r, POINTS_END_PENDING)
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
            "sequence_aa_length": r.sequence_aa_length,
            "point_multiplier": r.point_multiplier,
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
