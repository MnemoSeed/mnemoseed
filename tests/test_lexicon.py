"""v1 seed affective lexicon: data hygiene, coverage, and the loading seam.

The lexicon is hand-curated v1 DATA (NRC-style arousal/valence shapes, but not
NRC VAD itself, which is form-gated). These tests pin the data contract the
scorer consumes and the seam through which a calibrated resource can drop in.
"""

from __future__ import annotations

import pytest

from mnemoseed.capture.lexicon_v1 import (
    EN_LEXICON_V1,
    LEXICON_V1,
    ZH_LEXICON_V1,
    AffectiveEntry,
    Lexicon,
)


def test_entry_counts_meet_v1_floor() -> None:
    # a few hundred high-confidence entries per language
    assert len(EN_LEXICON_V1) >= 150
    assert len(ZH_LEXICON_V1) >= 150
    assert len(LEXICON_V1) == len(EN_LEXICON_V1) + len(ZH_LEXICON_V1)


def test_entries_have_valid_arid_ranges() -> None:
    for entry in LEXICON_V1:
        assert isinstance(entry.term, str) and entry.term
        assert 0.0 <= entry.arousal <= 1.0, entry.term
        assert -1.0 <= entry.valence <= 1.0, entry.term


def test_terms_are_unique_case_insensitively() -> None:
    seen: set[str] = set()
    for entry in LEXICON_V1:
        folded = entry.term.casefold()
        assert folded not in seen, f"duplicate term {entry.term!r}"
        seen.add(folded)


def test_developer_domain_covered() -> None:
    en_terms = {e.term.casefold() for e in EN_LEXICON_V1}
    zh_terms = {e.term for e in ZH_LEXICON_V1}
    assert {"frustrating", "broken", "refactor", "deploy", "crash"}.issubset(en_terms)
    assert {"崩溃", "报错", "卡死", "加班", "上线", "喜欢", "烦死了"}.issubset(zh_terms)


def test_default_lexicon_lookup_is_case_insensitive() -> None:
    lex = Lexicon()
    assert lex.lookup("LOVE") is not None
    assert lex.lookup("love") == lex.lookup("LOVE")
    assert lex.lookup("不存在词") is None


def test_loading_seam_accepts_replacement_data() -> None:
    custom = Lexicon(
        entries=(
            AffectiveEntry(term="自定义词", arousal=0.6, valence=0.2),
            AffectiveEntry(term="custom", arousal=0.9, valence=-0.8),
        )
    )
    assert custom.size == 2
    assert custom.lookup("custom") is not None
    assert custom.lookup("CUSTOM") is not None
    assert custom.lookup("love") is None


def test_lexicon_rejects_invalid_entry() -> None:
    with pytest.raises(ValueError):
        Lexicon(entries=(AffectiveEntry(term="bad", arousal=1.5, valence=0.0),))
    with pytest.raises(ValueError):
        Lexicon(entries=(AffectiveEntry(term="", arousal=0.5, valence=0.0),))


def test_scan_longest_match_consumes_span() -> None:
    lex = Lexicon()
    assert {e.term for e in lex.scan("烦死了")} == {"烦死了"}
    matched = {e.term for e in lex.scan("烦死了 不烦了")}
    assert "烦死了" in matched
    assert "烦" in matched


def test_scan_english_jammed_against_cjk_matches() -> None:
    # CJK ideographs are word-ish for Python `\b`; the scan must use ASCII
    # boundaries so an English term directly against CJK still matches.
    lex = Lexicon()
    matched = {e.term for e in lex.scan("这个bug真frustrating")}
    assert "bug" in matched
    assert "frustrating" in matched


def test_scan_english_spaced_variant_still_works() -> None:
    lex = Lexicon()
    matched = {e.term for e in lex.scan("这个 bug 真 frustrating")}
    assert "bug" in matched
    assert "frustrating" in matched


def test_scan_english_does_not_false_hit_subwords() -> None:
    # ASCII word semantics are preserved: a term never matches inside a longer
    # word, only as a standalone run of ASCII letters/digits.
    lex = Lexicon()
    terms = {e.term for e in lex.scan("debugging and debug")}
    assert "bug" not in terms
    assert "debug" in terms
    assert "debugging" in terms
