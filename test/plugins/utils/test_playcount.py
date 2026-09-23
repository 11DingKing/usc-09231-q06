import pytest

from beets import logging
from beets.library import Item
from beets.test.helper import TestHelper
from beetsplug._utils.playcount import (
    get_items,
    process_track,
    update_play_counts,
)

LOGGER_NAME = "beets.test_playcount"


class TestPlayCount(TestHelper):
    @pytest.fixture
    def log(self):
        log = logging.getLogger(LOGGER_NAME)
        log.set_global_level(logging.DEBUG)
        return log

    def track(self, **overrides):
        return {
            "mbid": "",
            "artist": "Artist",
            "name": "Song",
            "playcount": 1,
            **overrides,
        }

    def get_playcount(self, item_id, source="lastfm"):
        field = f"{source}_play_count"
        return int(self.lib.get_item(item_id).get(field, 0))

    def add_item(
        self,
        *,
        title="Song",
        artist="Artist",
        album="Album",
        mb_trackid="",
        play_count=None,
        source="lastfm",
    ):
        item = Item(
            title=title, artist=artist, album=album, mb_trackid=mb_trackid
        )
        self.lib.add(item)

        if play_count is not None:
            item[f"{source}_play_count"] = play_count
            item.store()

        return item

    @pytest.mark.parametrize(
        "item_kwargs, track_kwargs",
        [
            pytest.param(
                {"title": "Song", "artist": "Artist"},
                {"playcount": 7},
                id="artist-and-title",
            ),
            pytest.param(
                {
                    "title": "Different Song",
                    "artist": "Different Artist",
                    "mb_trackid": "track-id",
                },
                {"mbid": "track-id", "playcount": 5},
                id="musicbrainz-track-id",
            ),
            pytest.param(
                {
                    "title": "Song",
                    "artist": "Different Artist",
                    "album": "Album",
                },
                {"album": "Album", "playcount": 3},
                id="album-and-title",
            ),
            pytest.param(
                {"title": "Don\u2019t Stop", "artist": "Artist"},
                {"name": "Don't Stop", "playcount": 11},
                id="apostrophe-normalized",
            ),
        ],
    )
    def test_get_items_matches_supported_query_paths(
        self, log, item_kwargs, track_kwargs
    ):
        item = self.add_item(**item_kwargs)
        matched_ids = [
            matched.id
            for matched in get_items(self.lib, self.track(**track_kwargs), log)
        ]

        assert matched_ids == [item.id]

    def test_process_track_updates_every_matching_song(self, log):
        first = self.add_item(
            title="Song", artist="Artist", album="First Album", play_count=1
        )
        second = self.add_item(
            title="Song", artist="Artist", album="Second Album", play_count=9
        )

        assert (
            process_track(self.lib, self.track(playcount=0), log, "lastfm")
            is True
        )

        assert self.get_playcount(first.id) == 0
        assert self.get_playcount(second.id) == 0

    def test_process_track_prefers_album_when_titles_collide(self, log):
        # The same song title exists on two different albums and the
        # imported track names its album: only that copy is updated.
        on_album_a = self.add_item(
            title="Song", artist="Artist", album="Album A", play_count=1
        )
        on_album_b = self.add_item(
            title="Song", artist="Artist", album="Album B", play_count=9
        )

        assert (
            process_track(
                self.lib,
                self.track(album="Album A", playcount=5),
                log,
                "lastfm",
            )
            is True
        )

        assert self.get_playcount(on_album_a.id) == 5
        assert self.get_playcount(on_album_b.id) == 9

    def test_substring_does_not_shadow_mbid(self, log):
        # A fuzzy title match must not steal the count from the item
        # identified by the MusicBrainz track ID.
        mbid_item = self.add_item(
            title="Song (Live)",
            artist="Artist",
            album="Live Album",
            mb_trackid="mbid-1",
            play_count=1,
        )
        fuzzy_item = self.add_item(
            title="Song", artist="Artist", album="Album", play_count=9
        )

        assert (
            process_track(
                self.lib,
                self.track(mbid="mbid-1", name="Song", playcount=5),
                log,
                "lastfm",
            )
            is True
        )

        assert self.get_playcount(mbid_item.id) == 5
        assert self.get_playcount(fuzzy_item.id) == 9

    def test_mbid_match_takes_precedence_over_colliding_titles(self, log):
        # Even an exact title hit on another item loses to the MBID.
        target = self.add_item(
            title="Completely Different Title",
            artist="Artist",
            album="Album",
            mb_trackid="mbid-2",
            play_count=0,
        )
        decoy = self.add_item(
            title="Song", artist="Artist", album="Album", play_count=0
        )

        matched = get_items(
            self.lib, self.track(mbid="mbid-2", playcount=3), log
        )

        assert [item.id for item in matched] == [target.id]
        assert decoy.id not in [item.id for item in matched]

    def test_ambiguous_substring_match_is_skipped(self, log, caplog):
        # No MBID and no exact title: the substring matches two distinct
        # recordings, so neither may receive the imported count.
        live = self.add_item(
            title="Song (Live)", artist="Artist", play_count=1
        )
        remix = self.add_item(
            title="Song (Remix)", artist="Artist", play_count=2
        )

        with caplog.at_level("DEBUG", logger=LOGGER_NAME):
            assert (
                process_track(
                    self.lib,
                    self.track(name="Song", playcount=8),
                    log,
                    "lastfm",
                )
                is False
            )

        assert self.get_playcount(live.id) == 1
        assert self.get_playcount(remix.id) == 2
        assert any("ambiguous" in msg for msg in caplog.messages)

    def test_unique_substring_fallback_still_matches(self, log):
        # A single fuzzy candidate is an unambiguous enough fallback.
        item = self.add_item(
            title="Song (Live)", artist="Artist", play_count=1
        )

        assert (
            process_track(
                self.lib, self.track(name="Song", playcount=4), log, "lastfm"
            )
            is True
        )

        assert self.get_playcount(item.id) == 4

    def test_duplicate_rows_of_same_recording_all_update(self, log):
        # Rows that are the same recording (identical fields, no MBID)
        # are not treated as an ambiguous multi-recording conflict.
        first = self.add_item(
            title="Song (Live)", artist="Artist", album="Album", play_count=1
        )
        second = self.add_item(
            title="Song (Live)", artist="Artist", album="Album", play_count=2
        )

        assert (
            process_track(
                self.lib, self.track(name="Song", playcount=7), log, "lastfm"
            )
            is True
        )

        assert self.get_playcount(first.id) == 7
        assert self.get_playcount(second.id) == 7

    def test_missing_identifiers_uses_exact_fields(self, log):
        # Without an MBID (and without an album) an exact artist/title
        # match is still a reliable identity.
        item = self.add_item(
            title="Song", artist="Artist", album="Album", play_count=0
        )
        self.add_item(
            title="Song (Remix)", artist="Artist", album="Album", play_count=0
        )

        assert (
            process_track(
                self.lib,
                self.track(mbid="", playcount=6),
                log,
                "lastfm",
            )
            is True
        )

        assert self.get_playcount(item.id) == 6

    @pytest.mark.parametrize(
        "item_kwargs, track_kwargs",
        [
            pytest.param(
                {"title": "Song", "artist": "Artist", "album": "Album"},
                {"name": "song", "playcount": 1},
                id="case-insensitive-title",
            ),
            pytest.param(
                {"title": "Song", "artist": "Artist", "album": "Album"},
                {"artist": "artist", "playcount": 1},
                id="case-insensitive-artist",
            ),
            pytest.param(
                {"title": "Song", "artist": "Artist", "album": "Album"},
                {"album": "album", "playcount": 1},
                id="case-insensitive-album",
            ),
        ],
    )
    def test_exact_match_is_case_insensitive(
        self, log, item_kwargs, track_kwargs
    ):
        item = self.add_item(**item_kwargs)

        matched_ids = [
            matched.id
            for matched in get_items(self.lib, self.track(**track_kwargs), log)
        ]

        assert matched_ids == [item.id]

    def test_process_track_returns_false_when_nothing_matches(self, log):
        assert (
            process_track(
                self.lib,
                self.track(
                    artist="Missing Artist",
                    name="Missing Song",
                    album="Missing Album",
                    playcount=4,
                ),
                log,
                "lastfm",
            )
            is False
        )

    def test_process_track_updates_requested_source_field(self, log):
        new_count = 6
        item = self.add_item(play_count=1, source="lastfm")

        assert (
            process_track(
                self.lib, self.track(playcount=new_count), log, "listenbrainz"
            )
            is True
        )

        assert self.get_playcount(item.id, "lastfm") == 1
        assert self.get_playcount(item.id, "listenbrainz") == new_count

    @pytest.mark.parametrize(
        "tracks, expected_counts, expected_summary, expected_playcount",
        [
            pytest.param([], (0, 0), None, None, id="empty-page"),
            pytest.param(
                [
                    {"artist": "Artist", "name": "Known Song", "playcount": 8},
                    {
                        "artist": "Missing Artist",
                        "name": "Missing Song",
                        "playcount": 2,
                    },
                ],
                (1, 1),
                "Acquired 1/2 play-counts (1 unknown)",
                8,
                id="mixed-results",
            ),
        ],
    )
    def test_update_play_counts_counts_and_logs_summary(
        self,
        log,
        tracks,
        expected_counts,
        expected_summary,
        expected_playcount,
        caplog,
    ):
        item = self.add_item(title="Known Song", artist="Artist", play_count=1)

        with caplog.at_level("DEBUG", logger=LOGGER_NAME):
            assert (
                update_play_counts(
                    self.lib,
                    [self.track(**track) for track in tracks],
                    log,
                    "lastfm",
                )
                == expected_counts
            )

        assert any(
            f"Received {len(tracks)} tracks in this page, processing..." in msg
            for msg in caplog.messages
        )
        if expected_summary is None:
            assert not any("Acquired" in msg for msg in caplog.messages)
        else:
            assert expected_summary in caplog.text
            assert self.get_playcount(item.id) == expected_playcount
