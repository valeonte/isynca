"""Reading a date taken out of a file's name.

Some recorders never write a date into the file but put it in the name
instead: ``capture3.06-06-30_20-47.00.avi`` was recorded on 30 June 2006 at
20:47. A pattern in strftime notation says how the date is written, and is
looked for anywhere in the name, so ``%y-%m-%d_%H-%M.%S`` finds that one
without having to spell out the ``capture3.`` around it.

Only the numeric directives are understood -- ``%Y %y %m %d %H %M %S`` --
plus ``%%`` for a literal percent sign, and a pattern needs at least a year,
a month and a day. A name carries no timezone, so its date is read in the
zone given, or else in this machine's own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DIGITS: dict[str, str] = {
    "Y": r"\d{4}",
    "y": r"\d{2}",
    "m": r"\d{2}",
    "d": r"\d{2}",
    "H": r"\d{2}",
    "M": r"\d{2}",
    "S": r"\d{2}",
}
_REQUIRED = (frozenset("Yy"), frozenset("m"), frozenset("d"))
_DIRECTIVE = re.compile(r"%(.?)")


@dataclass(frozen=True, slots=True)
class NamePattern:
    """A compiled date pattern, and the zone its dates are read in."""

    pattern: str
    zone: tzinfo | None
    _search: re.Pattern[str]

    @classmethod
    def parse(cls, pattern: str, zone: tzinfo | None = None) -> NamePattern:
        """Compile ``pattern``.

        Raises:
            ValueError: The pattern uses a directive other than the numeric
                ones, or lacks a year, a month or a day.
        """
        parts: list[str] = []
        seen: set[str] = set()
        position = 0
        for match in _DIRECTIVE.finditer(pattern):
            parts.append(re.escape(pattern[position : match.start()]))
            directive = match.group(1)
            if directive == "%":
                parts.append("%")
            elif directive in _DIGITS:
                parts.append(_DIGITS[directive])
                seen.add(directive)
            else:
                raise ValueError(
                    f"%{directive} is not supported; use %Y %y %m %d %H %M %S"
                )
            position = match.end()
        parts.append(re.escape(pattern[position:]))
        if not all(seen & needed for needed in _REQUIRED):
            raise ValueError("needs at least a year (%Y or %y), a month and a day")
        return cls(pattern, zone, re.compile("".join(parts)))

    def date_in(self, path: Path) -> datetime | None:
        """Return the date in ``path``'s name, or ``None`` if there is none."""
        match = self._search.search(path.name)
        if match is None:
            return None
        try:
            # The name carries no zone; one is attached just below.
            naive = datetime.strptime(match.group(0), self.pattern)  # noqa: DTZ007
        except ValueError:
            # Digits in the right places that make no date, such as month 13.
            return None
        if self.zone is not None:
            return naive.replace(tzinfo=self.zone)
        return naive.astimezone()


def parse_timezone(text: str) -> tzinfo:
    """Return the zone ``text`` names: an offset like +03:00, or Europe/Athens.

    Raises:
        ValueError: ``text`` is neither.
    """
    try:
        # %z always yields a zone; the fallback only satisfies the type.
        return datetime.strptime(text, "%z").tzinfo or UTC
    except ValueError:
        pass
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"{text!r} is neither an offset like +03:00 nor a zone like Europe/Athens"
        ) from exc
