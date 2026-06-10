"""Table-driven tests for the match engine (spec phase-2).

Covers the spec's normalization table rows, the near-miss bands, and the
label-format checks used for 06-2016 records.
"""

import pytest

from server.pipeline.match import (
    Outcome,
    aggregate_outcomes,
    format_check_abv,
    format_check_net_contents,
    match_abv,
    match_class_type,
    match_name,
    match_net_contents,
    parse_abv,
    parse_net_contents,
)

# --- brand / fanciful name -------------------------------------------------

NAME_CASES = [
    # (form value, label text, expected outcome)
    ("STONE'S THROW", "Stone's Throw Winery", Outcome.EXACT),  # case/punct
    ("BARENJAGER", "Bärenjäger Honey Liqueur", Outcome.EXACT),  # accents
    ("VIEJO TONEL", "Pisco Viejo Tonel ICA-PERU", Outcome.EXACT),
    ("VIJO TONEL", "Viejo Tonel", Outcome.NEAR_MISS),  # spec table row
    ("CASCADE WINERY", "CASCADE WINERY", Outcome.EXACT),
    ("OLD CARTER", "OLD CARTER WHISKEY CO", Outcome.EXACT),
    ("BLACK MAPLE HILL", "BLAKC MAPEL HILL", Outcome.NEAR_MISS),  # OCR slips
    # A wholly different label scores so low it reads as "not found":
    # MISSING -> review, per escalate-on-doubt (never auto-fail on a
    # fuzzy non-match that could equally be unreadable OCR).
    ("HOWLING MOON", "Cascade Winery Table Red", Outcome.MISSING),
    ("PINDAR VINEYARDS", "", Outcome.MISSING),
]


@pytest.mark.parametrize("form_value,label_text,expected", NAME_CASES)
def test_match_name(form_value, label_text, expected):
    v = match_name("brand_name", form_value, [label_text])
    assert v.outcome == expected, (v.score, v.label_value)


def test_match_name_searches_all_crops():
    v = match_name("brand_name", "OLD CARTER", ["nothing here", "Old Carter Whiskey"])
    assert v.outcome == Outcome.EXACT


def test_match_name_normalized_tag():
    assert match_name("brand_name", "BARENJAGER", ["Bärenjäger"]).normalized
    assert not match_name("brand_name", "CASCADE", ["CASCADE WINERY"]).normalized


# --- net contents ----------------------------------------------------------

NET_PARSE_CASES = [
    ("750 MILLILITERS", 750.0),
    ("750 ml", 750.0),
    ("750ML", 750.0),
    ("1 LITER", 1000.0),
    ("1.75 Litres", 1750.0),
    ("75 cl", 750.0),
    ("12 FL. OZ", 12 * 29.5735),
    ("1/2 BARREL", 117348.0 / 2) if False else ("50 MILLILITERS", 50.0),
    ("no volume here", None),
]


@pytest.mark.parametrize("text,expected_ml", NET_PARSE_CASES)
def test_parse_net_contents(text, expected_ml):
    parsed = parse_net_contents(text)
    if expected_ml is None:
        assert parsed is None
    else:
        assert parsed is not None
        assert parsed[0] == pytest.approx(expected_ml, rel=1e-3)


NET_MATCH_CASES = [
    # spec table row: 750 MILLILITERS vs "750 ml" -> exact (normalized)
    ("750 MILLILITERS", ["Estate bottled 750 ml alc 13% by vol"], Outcome.EXACT),
    ("750 MILLILITERS", ["75 cl product of Italy"], Outcome.EXACT),  # unit conv
    ("750 MILLILITERS", ["375 ml"], Outcome.MISMATCH),
    ("750 MILLILITERS", ["no contents statement"], Outcome.MISSING),
    ("1 LITER", ["1000 ml"], Outcome.EXACT),
]


@pytest.mark.parametrize("form_value,label_texts,expected", NET_MATCH_CASES)
def test_match_net_contents(form_value, label_texts, expected):
    assert match_net_contents(form_value, label_texts).outcome == expected


def test_match_net_contents_normalized_tag():
    v = match_net_contents("750 MILLILITERS", ["750 ml"])
    assert v.normalized
    assert v.label_value == "750 ml"


# --- alcohol content --------------------------------------------------------

ABV_PARSE_CASES = [
    ("42", 42.0),  # bare form value
    ("42%", 42.0),
    ("11.5", 11.5),
    ("Alc. 42% by Vol", 42.0),
    ("ALC 13.5% BY VOL", 13.5),
    ("13,5% vol", 13.5),  # EU decimal comma
    ("80 PROOF", 40.0),
    ("115.8 proof", 57.9),
    ("ALCOHOL 16.5% BY VOLUME", 16.5),
    ("no abv here", None),
    ("vintage 2015", None),  # year must not parse as ABV
]


@pytest.mark.parametrize("text,expected", ABV_PARSE_CASES)
def test_parse_abv(text, expected):
    parsed = parse_abv(text)
    if expected is None:
        assert parsed is None
    else:
        assert parsed is not None
        assert parsed[0] == pytest.approx(expected)


ABV_MATCH_CASES = [
    # spec table row: form "42"/"42%" vs label "Alc. 42% by Vol" -> exact
    ("42", ["Pisco Italia Alc. 42% by Vol 750 ml"], Outcome.EXACT),
    ("42%", ["Alc. 42% by Vol"], Outcome.EXACT),
    ("46.5", ["92 proof? no: 93 proof"], Outcome.EXACT),  # proof conversion
    ("13.5", ["ALC. 12.5% BY VOL"], Outcome.MISMATCH),
    ("13.5", ["fine red wine"], Outcome.MISSING),
    # Implausible readings are OCR garbage -> doubt -> review, not fail.
    ("13.5", ["ALC. 00% BY VOL"], Outcome.MISSING),
]


@pytest.mark.parametrize("form_value,label_texts,expected", ABV_MATCH_CASES)
def test_match_abv(form_value, label_texts, expected):
    assert match_abv(form_value, label_texts).outcome == expected


# --- class / type ------------------------------------------------------------

CLASS_CASES = [
    # spec table row: description alias maps to the label term
    ("OTHER GRAPE BRANDY (PISCO, GRAPPA) FB", ["PISCO Viejo Tonel"], Outcome.EXACT),
    ("STRAIGHT BOURBON WHISKY", ["Straight Bourbon Whiskey"], Outcome.EXACT),
    ("TABLE RED WINE", ["Red Wine of California"], Outcome.EXACT),
    ("PORTER", ["Robust Porter Ale"], Outcome.EXACT),
    ("RYE WHISKY", ["nothing relevant"], Outcome.MISSING),
]


@pytest.mark.parametrize("description,label_texts,expected", CLASS_CASES)
def test_match_class_type(description, label_texts, expected):
    v = match_class_type(description, label_texts)
    assert v.outcome == expected, (v.score, v.label_value)


# --- 06-2016 label-format checks ---------------------------------------------


def test_format_checks_present_and_plausible():
    texts = ["Straight Bourbon Whiskey 750 ml Alc. 51.1% by Vol"]
    assert format_check_abv(texts).outcome == Outcome.EXACT
    assert format_check_net_contents(texts).outcome == Outcome.EXACT


def test_format_checks_missing():
    v = format_check_abv(["just a brand name"])
    assert v.outcome == Outcome.MISSING
    assert "not on form" in v.note
    assert format_check_net_contents(["just a brand name"]).outcome == Outcome.MISSING


def test_format_check_abv_implausible_reading_is_review_not_fail():
    v = format_check_abv(["ALC. 90% BY VOL table wine"])
    assert v.outcome == Outcome.MISSING
    assert "implausible" in v.note


def test_format_check_abv_prefers_plausible_reading():
    v = format_check_abv(["00% garbage then ALC. 13.5% BY VOL"])
    assert v.outcome == Outcome.EXACT


# --- aggregation --------------------------------------------------------------


def test_aggregate_outcomes():
    E, N, M, X = Outcome.EXACT, Outcome.NEAR_MISS, Outcome.MISMATCH, Outcome.MISSING
    assert aggregate_outcomes([E, E, E]) == "Pass"
    assert aggregate_outcomes([E, N, E]) == "Needs Review"
    assert aggregate_outcomes([E, X, E]) == "Needs Review"
    assert aggregate_outcomes([E, N, M]) == "Fail"
    assert aggregate_outcomes([]) == "Pass"
