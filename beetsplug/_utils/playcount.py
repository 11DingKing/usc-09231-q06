from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from typing_extensions import NotRequired

from beets.dbcore import AndQuery, MatchQuery, OrQuery
from beets.dbcore.query import StringQuery, SubstringQuery

if TYPE_CHECKING:
    from collections.abc import Sequence

    from beets.dbcore import Query
    from beets.dbcore.query import StringFieldQuery
    from beets.library import Item, Library
    from beets.logging import BeetsLogger


class Track(TypedDict):
    mbid: str | None
    name: str
    artist: str
    playcount: int
    album: NotRequired[str | None]


def _title_query(field_query: type[StringFieldQuery], title: str) -> Query:
    return OrQuery(
        [
            field_query("title", title),
            # try a right single quotation mark instead of an apostrophe
            field_query("title", title.replace("'", "’")),
        ]
    )


def _identity_key(item: Item) -> tuple[str, str]:
    """Normalized artist/title used to decide whether several items are
    copies of the same recording rather than conflicting candidates."""

    def normalize(value: object) -> str:
        return str(value).lower().replace("’", "'")

    return normalize(item.artist), normalize(item.title)


def _field_match(
    lib: Library,
    field_query: type[StringFieldQuery],
    artist: str,
    title: str,
    album: str,
) -> list[Item]:
    """Return items matching the complete fields, trying the most
    specific combination first so that an album name disambiguates
    recordings that share an artist and title."""
    title_query = _title_query(field_query, title)

    # Most specific first: artist + album, then artist, then album alone.
    candidate_queries: list[Query] = [
        AndQuery([title_query, field_query("artist", artist)])
    ]
    if album:
        candidate_queries.insert(
            0,
            AndQuery(
                [
                    title_query,
                    field_query("artist", artist),
                    field_query("album", album),
                ]
            ),
        )
        candidate_queries.append(
            AndQuery([title_query, field_query("album", album)])
        )

    for query in candidate_queries:
        items = list(lib.items(query))
        if items:
            return items
    return []


def _resolve_candidates(
    items: Sequence[Item],
    log: BeetsLogger,
    stage: str,
    artist: str,
    title: str,
) -> Sequence[Item]:
    """Guard against writing a play count to the wrong recording:
    candidates that differ in artist or title are an ambiguous match.
    Items sharing the normalized identity (e.g. the same recording on
    several albums) are all updated together."""
    identities = {_identity_key(item) for item in items}
    if len(identities) > 1:
        candidates = ", ".join(
            f"{item.artist} - {item.title} ({item.album})" for item in items
        )
        log.warning(
            "ambiguous {} match for {} - {}: conflicting candidates [{}],"
            " skipping play-count update",
            stage,
            artist,
            title,
            candidates,
        )
        return []
    return items


def get_items(lib: Library, track: Track, log: BeetsLogger) -> Sequence[Item]:
    mbid = track["mbid"]
    if mbid:
        items = list(lib.items(MatchQuery("mb_trackid", mbid)))
        if items:
            # The MusicBrainz recording id is authoritative; never let a
            # fuzzy title match override it.
            return items

    artist, title = track["artist"], track["name"]
    album = track.get("album") or ""

    log.debug("query: {} - {} ({})", artist, title, album)

    # Prefer whole-field, case-insensitive matches; only fall back to a
    # controlled substring search when no reliable field match exists.
    for stage, field_query in (
        ("exact", StringQuery),
        ("substring", SubstringQuery),
    ):
        items = _field_match(lib, field_query, artist, title, album)
        if items:
            return _resolve_candidates(items, log, stage, artist, title)

    return []


def process_track(
    lib: Library, track: Track, log: BeetsLogger, source: str
) -> bool:
    items = get_items(lib, track, log)
    if not items:
        return False

    new_count = track["playcount"]
    field = f"{source}_play_count"
    for song in items:
        count = int(song.get(field, 0))
        log.debug(
            "match: {0.artist} - {0.title} ({0.album}) updating:"
            " {1} {2} => {3}",
            song,
            field,
            count,
            new_count,
        )
        song[field] = new_count
        song.store()

    return True


def update_play_counts(
    lib: Library, tracks: Sequence[Track], log: BeetsLogger, source: str
) -> tuple[int, int]:
    total = len(tracks)
    total_found = 0
    total_fails = 0
    log.info("Received {} tracks in this page, processing...", total)

    with lib.transaction():
        for i, track in enumerate(tracks, 1):
            if i % 250 == 0:
                log.info("Processing track {}/{} ...", i, total)
            if process_track(lib, track, log, source):
                total_found += 1
            else:
                total_fails += 1

    if total_fails > 0:
        log.info(
            "Acquired {}/{} play-counts ({} unknown)",
            total_found,
            total,
            total_fails,
        )

    return total_found, total_fails
