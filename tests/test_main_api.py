import unittest

try:
    from fastapi import HTTPException
    from starlette.websockets import WebSocketDisconnect
except ModuleNotFoundError as exc:
    raise unittest.SkipTest("FastAPI is not installed") from exc

import game as gm
import main


class MainApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.GAMES.clear()
        main.SOCKETS.clear()
        for task in main.END_TASKS.values():
            task.cancel()
        main.END_TASKS.clear()
        for task in main.CLEANUP_TASKS.values():
            task.cancel()
        main.CLEANUP_TASKS.clear()

    async def asyncTearDown(self):
        main.GAMES.clear()
        main.SOCKETS.clear()
        for task in main.END_TASKS.values():
            task.cancel()
        main.END_TASKS.clear()
        for task in main.CLEANUP_TASKS.values():
            task.cancel()
        main.CLEANUP_TASKS.clear()

    async def test_create_game_endpoint_returns_host_and_public_config(self):
        response = await main.create_game(
            main.CreateGameBody(
                host_name="Host",
                expected_players=2,
                end_window_minutes=(1, 2),
            )
        )

        self.assertIn(response["game_id"], main.GAMES)
        game = main.GAMES[response["game_id"]]
        self.assertEqual(response["player_id"], game.host_player_id)
        self.assertEqual(response["host_player_id"], game.host_player_id)
        self.assertEqual(response["expected_players"], 2)
        self.assertEqual(response["mode"], "solo")
        self.assertEqual(response["codon_table"], game.codon_table)
        self.assertEqual(response["teams"], [])

    async def test_create_game_endpoint_validates_mode_and_game_rules(self):
        with self.assertRaises(HTTPException) as bad_mode:
            await main.create_game(
                main.CreateGameBody(host_name="Host", expected_players=2, mode="duo")
            )
        self.assertEqual(bad_mode.exception.status_code, 400)
        self.assertIn("Invalid mode", bad_mode.exception.detail)

        with self.assertRaises(HTTPException) as bad_teams:
            await main.create_game(
                main.CreateGameBody(
                    host_name="Host",
                    expected_players=2,
                    mode="teams",
                    teams=[main.TeamSpec(name="Only One")],
                )
            )
        self.assertEqual(bad_teams.exception.status_code, 400)
        self.assertIn("Team mode needs", bad_teams.exception.detail)

        with self.assertRaises(HTTPException) as bad_table:
            await main.create_game(
                main.CreateGameBody(
                    host_name="Host",
                    expected_players=2,
                    codon_table={"ATG": "NotAnAA"},
                )
            )
        self.assertEqual(bad_table.exception.status_code, 400)
        self.assertIn("Unsupported amino acid", bad_table.exception.detail)

    async def test_create_team_game_endpoint_serializes_teams(self):
        response = await main.create_game(
            main.CreateGameBody(
                host_name="Host",
                expected_players=2,
                mode="teams",
                teams=[
                    main.TeamSpec(name="Red", color="#f00"),
                    main.TeamSpec(name="Blue", color="#00f"),
                ],
            )
        )

        self.assertEqual(response["mode"], "teams")
        self.assertEqual([team["name"] for team in response["teams"]], ["Red", "Blue"])
        self.assertEqual([team["color"] for team in response["teams"]], ["#f00", "#00f"])

    async def test_join_endpoint_adds_player_and_rejoin_returns_same_identity(self):
        created = await main.create_game(
            main.CreateGameBody(host_name="Host", expected_players=2)
        )
        joined = await main.join_game(created["game_id"], main.JoinGameBody(name="Bob"))
        rejoined = await main.join_game(created["game_id"], main.JoinGameBody(name="bob"))

        self.assertEqual(joined["player_id"], rejoined["player_id"])
        self.assertEqual(joined["game_id"], created["game_id"])
        self.assertEqual(joined["host_player_id"], created["host_player_id"])
        self.assertEqual(joined["mode"], "solo")

    async def test_join_endpoint_404s_missing_game_and_400s_full_game(self):
        with self.assertRaises(HTTPException) as missing:
            await main.join_game("g_missing", main.JoinGameBody(name="Bob"))
        self.assertEqual(missing.exception.status_code, 404)

        created = await main.create_game(
            main.CreateGameBody(host_name="Host", expected_players=2)
        )
        await main.join_game(created["game_id"], main.JoinGameBody(name="Bob"))
        with self.assertRaises(HTTPException) as full:
            await main.join_game(created["game_id"], main.JoinGameBody(name="Cara"))
        self.assertEqual(full.exception.status_code, 400)
        self.assertIn("Game is full", full.exception.detail)

    async def test_get_codon_table_and_healthz(self):
        created = await main.create_game(
            main.CreateGameBody(host_name="Host", expected_players=2)
        )
        self.assertEqual(
            await main.get_codon_table(created["game_id"]),
            main.GAMES[created["game_id"]].codon_table,
        )
        self.assertEqual(await main.healthz(), {"status": "ok"})

        with self.assertRaises(HTTPException) as missing:
            await main.get_codon_table("g_missing")
        self.assertEqual(missing.exception.status_code, 404)

    async def test_broadcast_scores_sends_private_payload_to_each_socket(self):
        game, host = gm.new_game(
            "Host",
            2,
            (1, 1),
            mode=gm.Mode.TEAMS,
            teams=[{"name": "Red"}, {"name": "Blue"}],
        )
        guest = gm.join_game(game, "Guest")
        red_id, blue_id = list(game.teams)
        gm.assign_team(game, host.id, red_id)
        gm.assign_team(game, guest.id, blue_id)
        game.teams[red_id].score = 4.2
        game.teams[blue_id].score = -1.2
        main.GAMES[game.id] = game

        host_socket = FakeSocket()
        guest_socket = FakeSocket()
        main.SOCKETS[(game.id, host.id)] = {host_socket}
        main.SOCKETS[(game.id, guest.id)] = {guest_socket}

        await main._broadcast_scores(game)

        self.assertEqual(host_socket.messages, [{
            "type": "score_update",
            "score": {"scope": "team", "id": red_id, "name": "Red", "score": 4.2},
        }])
        self.assertEqual(guest_socket.messages, [{
            "type": "score_update",
            "score": {"scope": "team", "id": blue_id, "name": "Blue", "score": -1.2},
        }])

    async def test_websocket_reconnect_after_end_gets_final_reveal(self):
        game, host = gm.new_game("Host", 2, (1, 1))
        guest = gm.join_game(game, "Guest")
        gm.start_game(game, host.id)
        request = gm.create_request(game, host.id, "Protein", "ATGTAA")
        gm.submit_decryption(game, guest.id, request.id, "Met Stop")
        gm.confirm_decryption(game, host.id, request.id)
        gm.end_game(game)
        main.GAMES[game.id] = game

        socket = FakeSocket(disconnect_on_receive=True)
        await main.websocket_endpoint(socket, game.id, host.id)

        self.assertTrue(socket.accepted)
        self.assertEqual(socket.messages[0]["type"], "game_ended")
        self.assertEqual(socket.messages[0]["scoreboard"][0]["name"], "Guest")
        self.assertEqual(socket.messages[0]["requests"][0]["correct_peptide"], "Met-Stop")

    async def test_cleanup_game_later_removes_ended_games_and_sockets(self):
        game, host = gm.new_game("Host", 2, (1, 1))
        gm.join_game(game, "Guest")
        gm.start_game(game, host.id)
        gm.end_game(game)
        main.GAMES[game.id] = game
        main.SOCKETS[(game.id, host.id)] = {FakeSocket()}

        await main._cleanup_game_later(game.id, 0)

        self.assertNotIn(game.id, main.GAMES)
        self.assertFalse(any(key[0] == game.id for key in main.SOCKETS))


class FakeSocket:
    def __init__(self, disconnect_on_receive=False):
        self.messages = []
        self.accepted = False
        self.closed = False
        self.disconnect_on_receive = disconnect_on_receive

    async def accept(self):
        self.accepted = True

    async def send_json(self, payload):
        self.messages.append(payload)

    async def close(self):
        self.closed = True

    async def receive_text(self):
        if self.disconnect_on_receive:
            raise WebSocketDisconnect()
        return '{"type":"ping"}'


if __name__ == "__main__":
    unittest.main()
