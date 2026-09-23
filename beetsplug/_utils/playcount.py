from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from typing_extensions import NotRequired

from beets.dbcore import AndQuery, MatchQuery, OrQuery
from beets.dbcore.query import FieldQuery, StringQuery, SubstringQuery

if TYPE_CHECKING:
    from collections.abc import Sequence

    from beets.dbcore import Query
    from beets.library import Item, Library
    from beets.logging import BeetsLogger


class Track(TypedDict):
    mbid: str | None
    name: str
    artist: str
    playcount: int
    album: NotRequired[str | None]


def _title_query(query_cls: type[FieldQuery], title: str) -> Query:
    """Match ``title`` either verbatim or with a curly apostrophe."""
    return OrQuery(
        [
            query_cls("title", title),
            # try a right single quotation mark instead of an apostrophe
            query_cls("title", title.replace("'", "’")),
        ]
    )


def _recording_key(item: Item) -> tuple[str, ...]:
    """Identify the recording an item represents.

    Rows that share a key are duplicate imports of the same recording
    (e.g. the same track on several compilations); rows with different
    keys are distinct recordings that merely share a fuzzy title.
    """
    mbid = item.get("mb_trackid")
    if mbid:
        return ("mbid", str(mbid))
    return (
        "fields",
        str(item.title).casefold(),
        str(item.artist).casefold(),
        str(item.album or "").casefold(),
    )


def _resolve_fuzzy_items(
    items: Sequence[Item], log: BeetsLogger, artist: str, title: str
) -> list[Item]:
    """Guard the substring fallback against ambiguous matches.

    Substring queries can match several distinct recordings at once, so
    updating every hit would write the play count onto unrelated tracks.
    Rows that are the same recording (identical MBID or identical
    fields) are all updated; matches spanning several recordings are
    reported and skipped.
    """
    distinct = {_recording_key(item) for item in items}
    if len(distinct) > 1:
        log.warning(
            "ambiguous substring match for {} - {}: {} candidates refer to"
            " different recordings, skipping",
            artist,
            title,
            len(distinct),
        )
        return []
    return list(items)


def get_items(lib: Library, track: Track, log: BeetsLogger) -> Sequence[Item]:
    mbid, artist, title = track["mbid"], track["artist"], track["name"]
    album = track.get("album") or ""

    log.debug("query: {} - {} ({})", artist, title, album)

    # 1. The MusicBrainz track ID is an unambiguous identifier and always
    #    takes precedence over title-based matching, however strongly the
    #    title matches another item.
    if mbid:
        items = list(lib.items(MatchQuery("mb_trackid", mbid)))
        if items:
            # Prefer MBID if there is a match
            return items

    # 2. Whole-field exact matching (case-insensitive). When an album is
    #    known, narrow to it first so identically titled tracks on
    #    different albums do not receive each other's play counts.
    exact_title = _title_query(StringQuery, title)
    if album:
        items = list(
            lib.items(
                AndQuery(
                    [
                        StringQuery("artist", artist),
                        StringQuery("album", album),
                        exact_title,
                    ]
                )
            )
        )
        if items:
            return items

    items = list(
        lib.items(AndQuery([StringQuery("artist", artist), exact_title]))
    )
    if items:
        return items

    # 3. Substring fallback, used only when neither an MBID nor exact
    #    fields matched. The results are checked for ambiguity instead of
    #    being updated unconditionally.
    fuzzy_title = _title_query(SubstringQuery, title)
    identity_queries: list[Query] = [SubstringQuery("artist", artist)]
    if album:
        identity_queries.append(SubstringQuery("album", album))

    items = list(
        lib.items(AndQuery([fuzzy_title, OrQuery(identity_queries)]))
    )
    return _resolve_fuzzy_items(items, log, artist, title)


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
