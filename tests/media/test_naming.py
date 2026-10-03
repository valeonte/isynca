from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from isynca.media.naming import NamePattern, parse_timezone

ATHENS = ZoneInfo("Europe/Athens")
CAPTURE = "%y-%m-%d_%H-%M.%S"


def test_finds_the_date_anywhere_in_the_name():
    pattern = NamePattern.parse(CAPTURE, ATHENS)
    found = pattern.date_in(Path("/videos/capture3.06-06-30_20-47.00.avi"))
    assert found == datetime(2006, 6, 30, 20, 47, tzinfo=ATHENS)
    assert found is not None
    assert found.utcoffset() == timedelta(hours=3)


def test_a_zone_name_follows_daylight_saving():
    pattern = NamePattern.parse("%Y%m%d_%H%M", ATHENS)
    winter = pattern.date_in(Path("VID_20120122_1149.3gp"))
    assert winter is not None
    assert winter.utcoffset() == timedelta(hours=2)


def test_without_a_zone_the_date_is_local():
    found = NamePattern.parse("%Y-%m-%d").date_in(Path("scan 2011-12-27.jpg"))
    assert found == datetime(2011, 12, 27).astimezone()


def test_a_literal_percent_sign_matches_itself():
    pattern = NamePattern.parse("%%%Y%m%d")
    assert pattern.date_in(Path("100%20111227.jpg")) is not None
    assert pattern.date_in(Path("10020111227.jpg")) is None


def test_a_name_without_the_date_gives_none():
    assert NamePattern.parse(CAPTURE).date_in(Path("holiday.avi")) is None


def test_digits_that_make_no_date_give_none():
    assert NamePattern.parse(CAPTURE).date_in(Path("c.06-13-30_20-47.00.avi")) is None


def test_regex_characters_in_the_pattern_are_literal():
    pattern = NamePattern.parse("%Y.%m.%d")
    assert pattern.date_in(Path("2011x12x27.jpg")) is None
    assert pattern.date_in(Path("2011.12.27.jpg")) is not None


@pytest.mark.parametrize("bad", ["%H-%M", "%Y-%m", "%m-%d"])
def test_a_pattern_needs_a_year_a_month_and_a_day(bad):
    with pytest.raises(ValueError, match="needs at least a year"):
        NamePattern.parse(bad)


@pytest.mark.parametrize("bad", ["%Y-%b-%d", "%Y-%m-%d %"])
def test_only_numeric_directives_are_supported(bad):
    with pytest.raises(ValueError, match="is not supported"):
        NamePattern.parse(bad)


@pytest.mark.parametrize(
    ("text", "offset"),
    [("+03:00", timedelta(hours=3)), ("-0130", -timedelta(hours=1, minutes=30))],
)
def test_parses_an_offset(text, offset):
    assert parse_timezone(text) == timezone(offset)


def test_parses_a_zone_name():
    assert parse_timezone("Europe/Athens") == ATHENS


@pytest.mark.parametrize("bad", ["Atlantis/Nowhere", "+99:99", "", "../etc"])
def test_refuses_what_is_neither(bad):
    with pytest.raises(ValueError, match="neither an offset"):
        parse_timezone(bad)
