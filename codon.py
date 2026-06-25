"""
Codon table, translation, validation, and peptide canonicalization.

Pure functions only — no game state lives here.
"""

from __future__ import annotations

import re

# Reduced codon table — 10 codons, 9 amino acids + Stop.
# No two codons map to the same amino acid in this subset, so scoring
# is unambiguous against a canonical answer.
DEFAULT_CODON_TABLE: dict[str, str] = {
    "ATG": "Met",
    "GTT": "Val",
    "TTT": "Phe",
    "GAA": "Glu",
    "AAA": "Lys",
    "CCC": "Pro",
    "CAT": "His",
    "GGG": "Gly",
    "TGG": "Trp",
    "TAA": "Stop",
}

# Map of three-letter codes and accepted aliases (one-letter, common variants)
# used during peptide-guess canonicalization. All keys are uppercase.
AA_ALIASES: dict[str, str] = {
    "MET": "Met", "M": "Met",
    "VAL": "Val", "V": "Val",
    "PHE": "Phe", "F": "Phe",
    "GLU": "Glu", "E": "Glu",
    "LYS": "Lys", "K": "Lys",
    "PRO": "Pro", "P": "Pro",
    "HIS": "His", "H": "His",
    "GLY": "Gly", "G": "Gly",
    "TRP": "Trp", "W": "Trp",
    "STOP": "Stop", "*": "Stop", "X": "Stop",
}


def normalize_dna(dna: str) -> str:
    """Strip whitespace and uppercase. Does NOT validate — invalid DNA is allowed."""
    return re.sub(r"\s+", "", dna).upper()


def is_dna_valid(dna: str, codon_table: dict[str, str]) -> bool:
    """
    Server's internal validity check. A DNA sequence is valid iff:
      - non-empty,
      - contains only A/T/C/G,
      - length is a multiple of 3,
      - every triplet exists in the codon table.

    The decrypter never sees this flag. It only governs scoring when
    the decrypter rejects a request (invalid → requester loses 3).
    """
    cleaned = normalize_dna(dna)
    if not cleaned:
        return False
    if not re.fullmatch(r"[ATCG]+", cleaned):
        return False
    if len(cleaned) % 3 != 0:
        return False
    triplets = [cleaned[i : i + 3] for i in range(0, len(cleaned), 3)]
    return all(t in codon_table for t in triplets)


def translate_dna(dna: str, codon_table: dict[str, str]) -> str:
    """
    Translate DNA into the canonical peptide string used for comparison.

    Unknown triplets become '?'. Partial trailing triplets (length not
    divisible by 3) are dropped. The result is a hyphen-joined string of
    three-letter codes, e.g. 'Met-Val-Phe-Glu-Stop'.

    This is the server's source of truth for the "correct" peptide.
    """
    cleaned = normalize_dna(dna)
    aas: list[str] = []
    for i in range(0, len(cleaned) - len(cleaned) % 3, 3):
        triplet = cleaned[i : i + 3]
        aas.append(codon_table.get(triplet, "?"))
    return "-".join(aas)


def sequence_amino_acid_length(dna: str) -> int:
    """
    Count complete codon slots in a DNA sequence.

    This intentionally counts unknown triplets too, because invalid long
    requests should carry the same score stakes as valid long requests.
    Partial trailing bases do not produce an amino-acid slot.
    """
    return len(normalize_dna(dna)) // 3


def canonicalize_peptide(peptide: str) -> str:
    """
    Turn a player's free-form peptide guess into the canonical form
    'Met-Val-Phe-Glu-Stop' for comparison.

    Accepts: hyphens, en-dashes, em-dashes, spaces, commas, dots as
    separators; three-letter codes, one-letter codes, '*' or 'X' for stop,
    mixed case. Anything unrecognized becomes '?' so a typo doesn't
    silently match.
    """
    # Replace common separator variants with a single delimiter
    s = re.sub(r"[\s,\.\-\u2013\u2014]+", "|", peptide.strip())
    if not s:
        return ""
    tokens = [t for t in s.split("|") if t]
    canon: list[str] = []
    for tok in tokens:
        canon.append(AA_ALIASES.get(tok.upper(), "?"))
    return "-".join(canon)


def peptides_match(guess: str, truth: str) -> bool:
    """Compare a guess to the canonical truth, after canonicalizing the guess."""
    return canonicalize_peptide(guess) == truth
