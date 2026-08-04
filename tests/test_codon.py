import unittest

from codon import (
    DEFAULT_CODON_TABLE,
    canonicalize_peptide,
    is_dna_valid,
    normalize_codon_table,
    normalize_dna,
    peptides_match,
    sequence_amino_acid_length,
    translate_dna,
)


class CodonTests(unittest.TestCase):
    def test_default_table_has_unique_outputs_and_expected_entries(self):
        self.assertEqual(len(DEFAULT_CODON_TABLE), 10)
        self.assertEqual(len(set(DEFAULT_CODON_TABLE.values())), 10)
        self.assertEqual(DEFAULT_CODON_TABLE["ATG"], "Met")
        self.assertEqual(DEFAULT_CODON_TABLE["TAA"], "Stop")
        self.assertEqual(DEFAULT_CODON_TABLE["TGG"], "Trp")

    def test_normalize_dna_strips_whitespace_and_uppercases(self):
        self.assertEqual(normalize_dna(" atg\ngtt\t taa "), "ATGGTTTAA")

    def test_normalize_codon_table_cleans_aliases_and_rejects_unsupported_entries(self):
        self.assertEqual(
            normalize_codon_table({" atg ": "m", "taa": "*"}),
            {"ATG": "Met", "TAA": "Stop"},
        )
        invalid_tables = [
            {},
            {"AUG": "Met"},
            {"ATG": "Foo"},
            {"ATG": 1},
            {1: "Met"},
        ]
        for table in invalid_tables:
            with self.subTest(table=table):
                with self.assertRaises(ValueError):
                    normalize_codon_table(table)  # type: ignore[arg-type]

    def test_is_dna_valid_accepts_only_nonempty_known_complete_triplets(self):
        valid_cases = [
            "ATG",
            "atg gtt taa",
            "ATGGTTTTTGAA",
        ]
        invalid_cases = [
            "",
            "   ",
            "AT",
            "ATGX",
            "ATGZZZ",
            "ATGCGT",  # CGT is a real biological codon but not in this game table.
        ]
        for dna in valid_cases:
            with self.subTest(dna=dna):
                self.assertTrue(is_dna_valid(dna, DEFAULT_CODON_TABLE))
        for dna in invalid_cases:
            with self.subTest(dna=dna):
                self.assertFalse(is_dna_valid(dna, DEFAULT_CODON_TABLE))

    def test_translate_dna_returns_canonical_peptide_and_drops_partial_tail(self):
        self.assertEqual(
            translate_dna("ATG GTT TTT GAA TAA", DEFAULT_CODON_TABLE),
            "Met-Val-Phe-Glu-Stop",
        )
        self.assertEqual(
            translate_dna("ATGZZZG", DEFAULT_CODON_TABLE),
            "Met-?",
        )

    def test_sequence_amino_acid_length_counts_complete_slots_even_when_invalid(self):
        cases = {
            "": 0,
            "AT": 0,
            "ATG": 1,
            "ATGZZZ": 2,
            "ATGZZZG": 2,
            "ATG GTT TAA": 3,
        }
        for dna, expected in cases.items():
            with self.subTest(dna=dna):
                self.assertEqual(sequence_amino_acid_length(dna), expected)

    def test_canonicalize_peptide_accepts_aliases_and_separator_variants(self):
        cases = {
            "met val phe glu stop": "Met-Val-Phe-Glu-Stop",
            "M,V.F-E-*": "Met-Val-Phe-Glu-Stop",
            "m v f e x": "Met-Val-Phe-Glu-Stop",
            "Met--Val,,,Tyr": "Met-Val-?",
            "": "",
            "   ": "",
        }
        for peptide, expected in cases.items():
            with self.subTest(peptide=peptide):
                self.assertEqual(canonicalize_peptide(peptide), expected)

    def test_peptides_match_compares_after_canonicalization(self):
        truth = "Met-Val-Phe-Glu-Stop"
        self.assertTrue(peptides_match("M V F E *", truth))
        self.assertTrue(peptides_match("met-val-phe-glu-stop", truth))
        self.assertFalse(peptides_match("Met-Val-Phe-Gly-Stop", truth))
        self.assertFalse(peptides_match("Met-Val-Phe-Glu", truth))


if __name__ == "__main__":
    unittest.main()
