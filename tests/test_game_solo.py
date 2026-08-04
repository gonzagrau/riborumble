import unittest
from unittest.mock import patch

import game as gm


def make_started_solo_game():
    game, alice = gm.new_game("Alice", 2, (1, 1))
    bob = gm.join_game(game, "Bob")
    gm.start_game(game, alice.id)
    return game, alice, bob


def submit_assigned(game, request, guess):
    assert request.decrypter_id is not None
    return gm.submit_decryption(game, request.decrypter_id, request.id, guess)


class SoloLifecycleTests(unittest.TestCase):
    def test_new_game_validates_inputs(self):
        with self.assertRaisesRegex(ValueError, "Need at least 2 players"):
            gm.new_game("Alice", 1, 1)
        with self.assertRaisesRegex(ValueError, "Need at most 100 players"):
            gm.new_game("Alice", 101, 1)
        with self.assertRaisesRegex(ValueError, "Invalid game duration"):
            gm.new_game("Alice", 2, 0)
        with self.assertRaisesRegex(ValueError, "single fixed"):
            gm.new_game("Alice", 2, (1, 2))
        with self.assertRaisesRegex(ValueError, "Host name required"):
            gm.new_game("   ", 2, 1)

    def test_join_game_handles_rejoin_capacity_and_started_game(self):
        game, alice = gm.new_game("Alice", 2, (1, 1))
        bob = gm.join_game(game, "Bob")
        self.assertIs(gm.join_game(game, "bob"), bob)
        cara = gm.join_game(game, "Cara")
        self.assertEqual(cara.name, "Cara")

        gm.start_game(game, alice.id)
        self.assertIs(gm.join_game(game, "ALICE"), alice)
        with self.assertRaisesRegex(ValueError, "Game already started"):
            gm.join_game(game, "Dana")

    def test_start_game_requires_host_lobby_and_two_players(self):
        game, alice = gm.new_game("Alice", 3, 5)
        with self.assertRaisesRegex(ValueError, "Need at least 2 players"):
            gm.start_game(game, alice.id)
        bob = gm.join_game(game, "Bob")
        with self.assertRaisesRegex(PermissionError, "Only the host"):
            gm.start_game(game, bob.id)
        with patch("game.time.time", return_value=1000.0):
            end_at = gm.start_game(game, alice.id)
        self.assertEqual(game.phase, gm.Phase.IN_PROGRESS)
        self.assertEqual(game.started_at, 1000.0)
        self.assertEqual(end_at, 1300.0)
        self.assertEqual(end_at, game.scheduled_end_at)
        with self.assertRaisesRegex(ValueError, "Game already started"):
            gm.start_game(game, alice.id)

    def test_create_request_normalizes_stakes_and_assigns_other_player(self):
        game, alice, bob = make_started_solo_game()
        request = gm.create_request(game, alice.id, " Kinase ", " ATG GTT TAA ")
        self.assertEqual(request.requester_id, alice.id)
        self.assertEqual(request.decrypter_id, bob.id)
        self.assertEqual(request.protein_name, "Kinase")
        self.assertEqual(request.correct_peptide, "Met-Val-Stop")
        self.assertTrue(request.is_dna_valid)
        self.assertEqual(request.sequence_aa_length, 3)
        self.assertEqual(request.point_multiplier, 1.3)
        self.assertEqual(request.state, gm.RequestState.AWAITING_DECRYPTION)

    def test_create_request_rejects_bad_phase_and_empty_fields(self):
        game, alice = gm.new_game("Alice", 2, (1, 1))
        with self.assertRaisesRegex(ValueError, "Game is not in progress"):
            gm.create_request(game, alice.id, "Protein", "ATG")
        bob = gm.join_game(game, "Bob")
        gm.start_game(game, alice.id)
        with self.assertRaisesRegex(ValueError, "Protein name required"):
            gm.create_request(game, alice.id, " ", "ATG")
        with self.assertRaisesRegex(ValueError, "DNA sequence required"):
            gm.create_request(game, alice.id, "Protein", " ")
        with self.assertRaisesRegex(ValueError, "Unknown requester"):
            gm.create_request(game, "p_missing", "Protein", "ATG")
        self.assertEqual(bob.name, "Bob")

    def test_create_request_caps_protein_and_dna_lengths(self):
        game, alice, _bob = make_started_solo_game()
        with self.assertRaisesRegex(ValueError, "Protein name must be at most"):
            gm.create_request(
                game, alice.id, "P" * (gm.MAX_PROTEIN_NAME_LENGTH + 1), "ATG"
            )
        with self.assertRaisesRegex(ValueError, "DNA sequence must be at most"):
            gm.create_request(game, alice.id, "Protein", "ATG" * 101)

    def test_player_names_and_peptide_guesses_are_capped(self):
        with self.assertRaisesRegex(ValueError, "Host name must be at most"):
            gm.new_game("A" * (gm.MAX_PLAYER_NAME_LENGTH + 1), 2, (1, 1))
        game, alice = gm.new_game("Alice", 2, (1, 1))
        with self.assertRaisesRegex(ValueError, "Name must be at most"):
            gm.join_game(game, "B" * (gm.MAX_PLAYER_NAME_LENGTH + 1))
        bob = gm.join_game(game, "Bob")
        gm.start_game(game, alice.id)
        request = gm.create_request(game, alice.id, "Protein", "ATG")
        with self.assertRaisesRegex(ValueError, "Peptide guess must be at most"):
            gm.submit_decryption(
                game, bob.id, request.id, "M" * (gm.MAX_PEPTIDE_LENGTH + 1)
            )

    def test_custom_codon_tables_are_normalized_and_restricted_to_supported_answers(self):
        game, alice = gm.new_game(
            "Alice",
            2,
            (1, 1),
            codon_table={" atg ": "m", "taa": "*"},
        )
        self.assertEqual(game.codon_table, {"ATG": "Met", "TAA": "Stop"})
        gm.join_game(game, "Bob")
        gm.start_game(game, alice.id)
        request = gm.create_request(game, alice.id, "Protein", "ATGTAA")
        self.assertEqual(request.correct_peptide, "Met-Stop")

        with self.assertRaisesRegex(ValueError, "Unsupported amino acid"):
            gm.new_game("Alice", 2, (1, 1), codon_table={"ATG": "Foo"})
        with self.assertRaisesRegex(ValueError, "Invalid codon"):
            gm.new_game("Alice", 2, (1, 1), codon_table={"AUG": "Met"})

    def test_submit_decryption_authorization_first_write_and_canonical_truth(self):
        game, alice, bob = make_started_solo_game()
        request = gm.create_request(game, alice.id, "Protein", "ATGGTTTAA")

        with self.assertRaisesRegex(PermissionError, "Not your request"):
            gm.submit_decryption(game, alice.id, request.id, "Met Val Stop")
        with self.assertRaisesRegex(ValueError, "Peptide guess required"):
            gm.submit_decryption(game, bob.id, request.id, " ")

        gm.submit_decryption(game, bob.id, request.id, "M V *")
        self.assertEqual(request.state, gm.RequestState.PENDING_APPROVAL)
        self.assertTrue(request.submitted_peptide_is_correct)
        self.assertEqual(request.submitted_by_id, bob.id)

        with self.assertRaisesRegex(ValueError, "not awaiting decryption"):
            gm.submit_decryption(game, bob.id, request.id, "Met-Val-Stop")

    def test_requester_resolution_requires_pending_request_and_authorization(self):
        game, alice, bob = make_started_solo_game()
        request = gm.create_request(game, alice.id, "Protein", "ATGGTTTAA")
        with self.assertRaisesRegex(ValueError, "not pending approval"):
            gm.confirm_decryption(game, alice.id, request.id)
        submit_assigned(game, request, "Met Val Stop")
        with self.assertRaisesRegex(PermissionError, "Not your request"):
            gm.confirm_decryption(game, bob.id, request.id)
        with self.assertRaisesRegex(ValueError, "Unknown request"):
            gm.confirm_decryption(game, alice.id, "r_missing")


class SoloScoringTableTests(unittest.TestCase):
    def assert_scores(self, game, expected):
        actual = {p.name: p.score for p in game.players.values()}
        self.assertEqual(actual, expected)

    def test_confirm_valid_correct_rewards_decrypter_and_requester(self):
        game, alice, bob = make_started_solo_game()
        request = gm.create_request(game, alice.id, "Protein", "ATGGTTTAA")
        submit_assigned(game, request, "M V *")
        resolved, deltas = gm.confirm_decryption(game, alice.id, request.id)

        self.assertIs(resolved, request)
        self.assertEqual(request.state, gm.RequestState.CONFIRMED)
        self.assertEqual(deltas, [(bob.id, 3.9), (alice.id, 1.3)])
        self.assert_scores(game, {"Alice": 1.3, "Bob": 3.9})

    def test_confirm_valid_wrong_penalizes_both_sides(self):
        game, alice, bob = make_started_solo_game()
        request = gm.create_request(game, alice.id, "Protein", "ATGGTTTAA")
        submit_assigned(game, request, "Met-Val-Phe")
        _, deltas = gm.confirm_decryption(game, alice.id, request.id)

        self.assertEqual(deltas, [(bob.id, -3.9), (alice.id, -3.9)])
        self.assert_scores(game, {"Alice": -3.9, "Bob": -3.9})

    def test_confirm_invalid_penalizes_missed_invalid_and_bad_accept(self):
        game, alice, bob = make_started_solo_game()
        request = gm.create_request(game, alice.id, "Bad Protein", "ATGZZZ")
        submit_assigned(game, request, "Met ?")
        _, deltas = gm.confirm_decryption(game, alice.id, request.id)

        self.assertFalse(request.is_dna_valid)
        self.assertEqual(request.point_multiplier, 1.2)
        self.assertEqual(deltas, [(bob.id, -3.6), (alice.id, -7.2)])
        self.assert_scores(game, {"Alice": -7.2, "Bob": -3.6})

    def test_reject_valid_correct_rewards_decrypter_and_penalizes_requester(self):
        game, alice, bob = make_started_solo_game()
        request = gm.create_request(game, alice.id, "Protein", "ATGGTTTAA")
        submit_assigned(game, request, "Met Val Stop")
        _, deltas = gm.reject_decryption(game, alice.id, request.id)

        self.assertEqual(request.state, gm.RequestState.REJECTED)
        self.assertEqual(deltas, [(bob.id, 3.9), (alice.id, -3.9)])
        self.assert_scores(game, {"Alice": -3.9, "Bob": 3.9})

    def test_reject_valid_wrong_penalizes_decrypter_and_rewards_requester_qc(self):
        game, alice, bob = make_started_solo_game()
        request = gm.create_request(game, alice.id, "Protein", "ATGGTTTAA")
        submit_assigned(game, request, "Met-Val-Phe")
        _, deltas = gm.reject_decryption(game, alice.id, request.id)

        self.assertEqual(deltas, [(bob.id, -3.9), (alice.id, 1.3)])
        self.assert_scores(game, {"Alice": 1.3, "Bob": -3.9})

    def test_reject_invalid_penalizes_missed_invalid_and_partly_offsets_requester_qc(self):
        game, alice, bob = make_started_solo_game()
        request = gm.create_request(game, alice.id, "Bad Protein", "ATGZZZ")
        submit_assigned(game, request, "Met ?")
        _, deltas = gm.reject_decryption(game, alice.id, request.id)

        self.assertEqual(deltas, [(bob.id, -3.6), (alice.id, -2.4)])
        self.assert_scores(game, {"Alice": -2.4, "Bob": -3.6})

    def test_flag_invalid_correct_closes_request_and_scores_detection(self):
        game, alice, bob = make_started_solo_game()
        request = gm.create_request(game, alice.id, "Bad Protein", "ATGZZZ")
        resolved, deltas = gm.flag_invalid_request(game, bob.id, request.id)

        self.assertIs(resolved, request)
        self.assertEqual(request.state, gm.RequestState.FLAGGED_INVALID)
        self.assertEqual(request.submitted_peptide, "Invalid DNA")
        self.assertTrue(request.submitted_peptide_is_correct)
        self.assertEqual(request.resolved_by_id, bob.id)
        self.assertEqual(deltas, [(bob.id, 1.2), (alice.id, -3.6)])
        self.assert_scores(game, {"Alice": -3.6, "Bob": 1.2})

    def test_flag_invalid_false_closes_request_and_penalizes_decrypter(self):
        game, alice, bob = make_started_solo_game()
        request = gm.create_request(game, alice.id, "Protein", "ATGTAA")
        _, deltas = gm.flag_invalid_request(game, bob.id, request.id)

        self.assertEqual(request.state, gm.RequestState.FLAGGED_INVALID)
        self.assertFalse(request.submitted_peptide_is_correct)
        self.assertEqual(deltas, [(bob.id, -3.6)])
        self.assert_scores(game, {"Alice": 0.0, "Bob": -3.6})

    def test_flag_invalid_requires_designated_decrypter_and_awaiting_state(self):
        game, alice, bob = make_started_solo_game()
        request = gm.create_request(game, alice.id, "Protein", "ATGTAA")
        with self.assertRaisesRegex(PermissionError, "Not your request"):
            gm.flag_invalid_request(game, alice.id, request.id)
        gm.submit_decryption(game, bob.id, request.id, "Met Stop")
        with self.assertRaisesRegex(ValueError, "not awaiting decryption"):
            gm.flag_invalid_request(game, bob.id, request.id)

    def test_end_game_sweeps_awaiting_and_pending_with_multipliers(self):
        game, alice = gm.new_game("Alice", 3, (1, 1))
        bob = gm.join_game(game, "Bob")
        cara = gm.join_game(game, "Cara")
        gm.start_game(game, alice.id)

        awaiting = gm.create_request(game, alice.id, "Awaiting", "ATGGTTTTTGAA")
        pending = gm.create_request(game, bob.id, "Pending", "ATGTAA")
        gm.submit_decryption(game, pending.decrypter_id, pending.id, "Met Stop")
        reveal = gm.end_game(game)

        self.assertEqual(game.phase, gm.Phase.ENDED)
        self.assertEqual(
            {(item["request_id"], item["delta"], item["reason"]) for item in reveal["sweep"]},
            {
                (awaiting.id, -2.8, "left_undecrypted"),
                (pending.id, 2.4, "left_unresolved_correct"),
                (pending.id, -2.4, "left_unresolved_qc"),
            },
        )
        self.assertEqual(awaiting.state, gm.RequestState.AWAITING_DECRYPTION)
        self.assertEqual(pending.state, gm.RequestState.PENDING_APPROVAL)
        self.assertAlmostEqual(sum(p.score for p in [alice, bob, cara]), -2.8)

    def test_end_game_is_idempotent_and_reveal_includes_truth_and_stakes(self):
        game, alice, bob = make_started_solo_game()
        request = gm.create_request(game, alice.id, "Protein", "ATGGTTTAA")
        submit_assigned(game, request, "Met Val Stop")
        gm.confirm_decryption(game, alice.id, request.id)

        reveal1 = gm.end_game(game)
        scores_after_first = {p.id: p.score for p in game.players.values()}
        reveal2 = gm.end_game(game)
        self.assertEqual(scores_after_first, {p.id: p.score for p in game.players.values()})
        self.assertNotIn("sweep", reveal2)

        row = reveal1["requests"][0]
        self.assertEqual(row["correct_peptide"], "Met-Val-Stop")
        self.assertTrue(row["is_dna_valid"])
        self.assertEqual(row["sequence_aa_length"], 3)
        self.assertEqual(row["point_multiplier"], 1.3)
        self.assertEqual(row["state"], "confirmed")

    def test_state_snapshot_hides_truth_but_exposes_private_score_and_own_stacks(self):
        game, alice, bob = make_started_solo_game()
        request = gm.create_request(game, alice.id, "Protein", "ATGGTTTAA")
        submit_assigned(game, request, "Met Val Stop")
        gm.confirm_decryption(game, alice.id, request.id)

        alice_snapshot = gm.state_snapshot_for(game, alice.id)
        bob_snapshot = gm.state_snapshot_for(game, bob.id)
        self.assertEqual(alice_snapshot["score"]["score"], 1.3)
        self.assertEqual(bob_snapshot["score"]["score"], 3.9)
        self.assertEqual(len(alice_snapshot["outgoing"]), 1)
        self.assertEqual(len(alice_snapshot["incoming"]), 0)
        self.assertEqual(len(bob_snapshot["incoming"]), 1)
        self.assertNotIn("correct_peptide", alice_snapshot["outgoing"][0])
        self.assertNotIn("is_dna_valid", bob_snapshot["incoming"][0])


if __name__ == "__main__":
    unittest.main()
