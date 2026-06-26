import unittest

import game as gm


def make_started_team_game():
    game, host = gm.new_game(
        "Host",
        4,
        (1, 1),
        mode=gm.Mode.TEAMS,
        teams=[
            {"name": "Red", "color": "#f85149"},
            {"name": "Blue", "color": "#58a6ff"},
        ],
    )
    red_mate = gm.join_game(game, "Red Mate")
    blue_one = gm.join_game(game, "Blue One")
    blue_two = gm.join_game(game, "Blue Two")
    red_id, blue_id = list(game.teams)
    gm.assign_team(game, host.id, red_id)
    gm.assign_team(game, red_mate.id, red_id)
    gm.assign_team(game, blue_one.id, blue_id)
    gm.assign_team(game, blue_two.id, blue_id)
    gm.start_game(game, host.id)
    return game, host, red_mate, blue_one, blue_two, red_id, blue_id


class TeamSetupTests(unittest.TestCase):
    def test_team_game_validates_team_configuration(self):
        with self.assertRaisesRegex(ValueError, "Team mode needs at least 2 teams"):
            gm.new_game("Host", 2, (1, 1), mode=gm.Mode.TEAMS, teams=None)
        with self.assertRaisesRegex(ValueError, "Team mode needs at least 2 teams"):
            gm.new_game(
                "Host",
                2,
                (1, 1),
                mode=gm.Mode.TEAMS,
                teams=[{"name": "Red", "color": "#f00"}],
            )
        with self.assertRaisesRegex(ValueError, "Team name required"):
            gm.new_game(
                "Host",
                2,
                (1, 1),
                mode=gm.Mode.TEAMS,
                teams=[{"name": "Red"}, {"name": " "}],
            )
        with self.assertRaisesRegex(ValueError, "Team color must be a hex color"):
            gm.new_game(
                "Host",
                2,
                (1, 1),
                mode=gm.Mode.TEAMS,
                teams=[
                    {"name": "Red", "color": 'red" onmouseover="alert(1)'},
                    {"name": "Blue", "color": "#00f"},
                ],
            )
        with self.assertRaisesRegex(ValueError, "Team name must be at most"):
            gm.new_game(
                "Host",
                2,
                (1, 1),
                mode=gm.Mode.TEAMS,
                teams=[
                    {"name": "R" * (gm.MAX_TEAM_NAME_LENGTH + 1)},
                    {"name": "Blue"},
                ],
            )

    def test_assign_team_only_in_team_lobby_with_known_player_and_team(self):
        solo, solo_host = gm.new_game("Solo Host", 2, (1, 1))
        with self.assertRaisesRegex(ValueError, "Not a team-mode game"):
            gm.assign_team(solo, solo_host.id, "t_missing")

        game, host = gm.new_game(
            "Host",
            2,
            (1, 1),
            mode=gm.Mode.TEAMS,
            teams=[{"name": "Red"}, {"name": "Blue"}],
        )
        red_id = next(iter(game.teams))
        with self.assertRaisesRegex(ValueError, "Unknown player"):
            gm.assign_team(game, "p_missing", red_id)
        with self.assertRaisesRegex(ValueError, "Unknown team"):
            gm.assign_team(game, host.id, "t_missing")
        gm.assign_team(game, host.id, red_id)
        self.assertEqual(host.team_id, red_id)

    def test_team_start_requires_assignments_and_two_occupied_teams(self):
        game, host = gm.new_game(
            "Host",
            3,
            (1, 1),
            mode=gm.Mode.TEAMS,
            teams=[{"name": "Red"}, {"name": "Blue"}, {"name": "Green"}],
        )
        p2 = gm.join_game(game, "P2")
        p3 = gm.join_game(game, "P3")
        red_id, blue_id, green_id = list(game.teams)
        gm.assign_team(game, host.id, red_id)
        gm.assign_team(game, p2.id, red_id)

        with self.assertRaisesRegex(ValueError, "Players without team"):
            gm.start_game(game, host.id)
        gm.assign_team(game, p3.id, red_id)
        with self.assertRaisesRegex(ValueError, "at least 2 teams"):
            gm.start_game(game, host.id)
        gm.assign_team(game, p3.id, blue_id)
        gm.start_game(game, host.id)
        self.assertEqual(game.players[p3.id].team_id, blue_id)
        self.assertEqual(game.teams[green_id].score, 0.0)
        with self.assertRaisesRegex(ValueError, "Teams can only be picked"):
            gm.assign_team(game, p2.id, blue_id)


class TeamGameplayTests(unittest.TestCase):
    def test_team_request_routes_to_opposing_team_and_shared_snapshots(self):
        game, host, red_mate, blue_one, blue_two, red_id, blue_id = make_started_team_game()
        request = gm.create_request(game, host.id, "Team Protein", "ATGTAA")

        self.assertEqual(request.requester_team_id, red_id)
        self.assertEqual(request.decrypter_team_id, blue_id)
        self.assertIsNone(request.decrypter_id)

        red_snapshot = gm.state_snapshot_for(game, red_mate.id)
        blue_snapshot = gm.state_snapshot_for(game, blue_two.id)
        self.assertEqual(len(red_snapshot["outgoing"]), 1)
        self.assertEqual(len(red_snapshot["incoming"]), 0)
        self.assertEqual(len(blue_snapshot["incoming"]), 1)
        self.assertEqual(len(blue_snapshot["outgoing"]), 0)

    def test_team_request_ignores_unoccupied_team_choices(self):
        game, host = gm.new_game(
            "Host",
            4,
            (1, 1),
            mode=gm.Mode.TEAMS,
            teams=[{"name": "Red"}, {"name": "Blue"}, {"name": "Green"}],
        )
        guest = gm.join_game(game, "Guest")
        red_id, blue_id, green_id = list(game.teams)
        gm.assign_team(game, host.id, red_id)
        gm.assign_team(game, guest.id, blue_id)
        gm.start_game(game, host.id)

        request = gm.create_request(game, host.id, "Team Protein", "ATGTAA")
        reveal = gm.end_game(game)

        self.assertEqual(request.decrypter_team_id, blue_id)
        self.assertNotEqual(request.decrypter_team_id, green_id)
        self.assertEqual([t["id"] for t in reveal["team_scoreboard"]], [red_id, blue_id])

    def test_any_decrypter_teammate_can_submit_and_first_write_wins(self):
        game, host, _red_mate, blue_one, blue_two, _red_id, _blue_id = make_started_team_game()
        request = gm.create_request(game, host.id, "Team Protein", "ATGTAA")

        gm.submit_decryption(game, blue_one.id, request.id, "Met Stop")
        self.assertEqual(request.submitted_by_id, blue_one.id)
        self.assertTrue(request.submitted_peptide_is_correct)
        with self.assertRaisesRegex(ValueError, "not awaiting decryption"):
            gm.submit_decryption(game, blue_two.id, request.id, "Met Stop")

    def test_non_decrypter_team_cannot_submit_or_flag(self):
        game, host, red_mate, _blue_one, _blue_two, _red_id, _blue_id = make_started_team_game()
        request = gm.create_request(game, host.id, "Team Protein", "ATGTAA")
        with self.assertRaisesRegex(PermissionError, "Not your team's request"):
            gm.submit_decryption(game, red_mate.id, request.id, "Met Stop")
        with self.assertRaisesRegex(PermissionError, "Not your team's request"):
            gm.flag_invalid_request(game, red_mate.id, request.id)

    def test_any_requester_teammate_can_resolve_and_scores_team_ledger(self):
        game, host, red_mate, blue_one, _blue_two, red_id, blue_id = make_started_team_game()
        request = gm.create_request(game, host.id, "Team Protein", "ATGTAA")
        gm.submit_decryption(game, blue_one.id, request.id, "Met Stop")
        _, deltas = gm.confirm_decryption(game, red_mate.id, request.id)

        self.assertEqual(request.resolved_by_id, red_mate.id)
        self.assertEqual(deltas, [(blue_id, 3.6), (red_id, 1.2)])
        self.assertEqual(game.teams[blue_id].score, 3.6)
        self.assertEqual(game.teams[red_id].score, 1.2)
        self.assertEqual(host.score, 0.0)
        self.assertEqual(blue_one.score, 0.0)

    def test_non_requester_team_cannot_resolve(self):
        game, host, _red_mate, blue_one, _blue_two, _red_id, _blue_id = make_started_team_game()
        request = gm.create_request(game, host.id, "Team Protein", "ATGTAA")
        gm.submit_decryption(game, blue_one.id, request.id, "Met Stop")
        with self.assertRaisesRegex(PermissionError, "Not your team's request to resolve"):
            gm.confirm_decryption(game, blue_one.id, request.id)

    def test_team_invalid_flag_scores_team_ledger_and_private_score_views(self):
        game, host, _red_mate, blue_one, blue_two, red_id, blue_id = make_started_team_game()
        request = gm.create_request(game, host.id, "Bad Team Protein", "ATGZZZ")
        _, deltas = gm.flag_invalid_request(game, blue_two.id, request.id)

        self.assertEqual(deltas, [(blue_id, 1.2), (red_id, -3.6)])
        self.assertEqual(game.teams[blue_id].score, 1.2)
        self.assertEqual(game.teams[red_id].score, -3.6)
        self.assertEqual(gm.score_view_for(game, host.id), {
            "scope": "team",
            "id": red_id,
            "name": "Red",
            "score": -3.6,
        })
        self.assertEqual(gm.score_view_for(game, blue_one.id), {
            "scope": "team",
            "id": blue_id,
            "name": "Blue",
            "score": 1.2,
        })

    def test_team_end_sweep_charges_or_rewards_decrypter_team(self):
        game, host, _red_mate, blue_one, _blue_two, red_id, blue_id = make_started_team_game()
        awaiting = gm.create_request(game, host.id, "Awaiting", "ATGTAA")
        pending = gm.create_request(game, blue_one.id, "Pending", "ATGGTTTAA")
        gm.submit_decryption(game, host.id, pending.id, "Met Val Stop")

        reveal = gm.end_game(game)
        self.assertEqual(game.teams[blue_id].score, -5.0)
        self.assertEqual(game.teams[red_id].score, 2.6)
        self.assertEqual(
            {(item["request_id"], item["delta"], item["reason"]) for item in reveal["sweep"]},
            {
                (awaiting.id, -2.4, "left_undecrypted"),
                (pending.id, 2.6, "left_unresolved_correct"),
                (pending.id, -2.6, "left_unresolved_qc"),
            },
        )

    def test_final_reveal_includes_team_scoreboard_sorted_by_score(self):
        game, host, _red_mate, blue_one, _blue_two, red_id, blue_id = make_started_team_game()
        request = gm.create_request(game, host.id, "Team Protein", "ATGTAA")
        gm.submit_decryption(game, blue_one.id, request.id, "Met Stop")
        gm.confirm_decryption(game, host.id, request.id)
        reveal = gm.end_game(game)

        self.assertEqual(reveal["mode"], "teams")
        self.assertEqual([team["id"] for team in reveal["team_scoreboard"]], [blue_id, red_id])
        self.assertEqual(reveal["team_scoreboard"][0]["score"], 3.6)
        self.assertEqual(reveal["scoreboard"][0]["score"], 0.0)
        row = reveal["requests"][0]
        self.assertEqual(row["requester_team_id"], red_id)
        self.assertEqual(row["decrypter_team_id"], blue_id)
        self.assertEqual(row["submitted_by_id"], blue_one.id)


if __name__ == "__main__":
    unittest.main()
