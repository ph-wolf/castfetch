import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from castfetch import cli, config, reports, search, storage, sync, transcription
from castfetch.sources import instagram, podcast

VTT = b"WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nGuest: Local robots are useful.\n\n00:00:04.000 --> 00:00:07.000\nHost: What about renewable\n\n00:00:07.000 --> 00:00:10.000\nenergy in factories?\n"


def episode(key="one", published="2026-11-20T12:00:00+00:00", official=True):
    return {
        "episode_key": key,
        "title": "A fictional interview",
        "episode_number": key,
        "published": published,
        "episode_url": "https://example.com/episode/" + key,
        "audio_url": "https://example.com/audio.mp3",
        "publisher_transcripts_advertised": official,
        "transcripts": [{"source_url": f"https://example.com/{key}.vtt", "mime_type": "text/vtt"}]
        if official
        else [],
    }


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.file = self.base / "config.yaml"
        self.file.write_text("""version: 1
archive_root: archive
logging:
  directory: logs
sources:
  - id: example-show
    name: Example Show
    type: podcast
    feed_url: https://example.com/feed.xml
    initial_sync: all
""")
        self.cfg = config.load(self.file)
        self.source = self.cfg.sources[0]

    def sync(self, episodes, source=None, dry=False, backfill=None, error=None):
        with (
            patch.object(podcast, "discover", return_value=(episodes, {}, b"<rss/>")),
            patch.object(
                sync.network,
                "fetch",
                side_effect=error,
                return_value=(VTT, "https://example.com/transcript.vtt"),
            ),
        ):
            return sync.synchronize(source or self.source, self.cfg, dry, backfill)

    def install_episode(self, ep=None):
        ep = ep or episode()
        self.assertEqual(self.sync([ep])["downloaded"], 1)
        row = storage.index_read(self.cfg.archive_root, self.source.id)[ep["episode_key"]]
        return row, storage.inside(self.cfg.archive_root, row["transcripts"][0]["local_path"])


class ConfigTests(Fixture):
    def test_python_symlink_keeps_virtual_environment_context(self):
        interpreter = self.base / "venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to("/bin/sh")
        self.file.write_text(self.file.read_text() + f"\ntranscription:\n  python: {interpreter}\n")
        cfg = config.load(self.file)
        self.assertEqual(cfg.transcription["python"], str(interpreter))
        self.assertNotEqual(cfg.transcription["python"], str(interpreter.resolve()))

    def test_relative_paths_and_defaults(self):
        self.assertEqual(self.cfg.archive_root, self.base / "archive")
        self.assertEqual(self.cfg.logging_directory, self.base / "logs")
        self.assertEqual(self.source.transcription, "publisher")
        self.assertIsNone(self.source.initial_days)

    def test_example_is_valid_fictional_disabled_and_documents_both_sources(self):
        cfg = config.load(Path(__file__).parents[1] / "config.example.yaml")
        self.assertEqual({s.type for s in cfg.sources}, {"podcast", "instagram"})
        self.assertTrue(all(not s.enabled for s in cfg.sources))

    def test_unknown_duplicate_and_null_keys(self):
        original = self.file.read_text()
        for addition in ("\nunknown: true\n", "\nversion: 1\n", "\nlogging: null\n"):
            self.file.write_text(original + addition)
            with self.subTest(addition=addition), self.assertRaises(config.ConfigError):
                config.load(self.file)

    def test_bad_source_options(self):
        original = self.file.read_text()
        for changed in (
            original.replace("type: podcast", "type: video"),
            original.replace("initial_sync: all", "initial_sync: {days: 0}"),
            original.replace("initial_sync: all", 'enabled: "false"'),
            original.replace("initial_sync: all", "transcription: always"),
            original.replace("example-show", "../escape"),
        ):
            self.file.write_text(changed)
            with self.subTest(changed=changed), self.assertRaises(config.ConfigError):
                config.load(self.file)

    def test_discovery_and_explicit_missing(self):
        with patch("pathlib.Path.cwd", return_value=self.base):
            self.assertEqual(config.locate(), self.file)
        with self.assertRaises(config.ConfigError):
            config.locate(self.base / "missing")


class StorageTests(Fixture):
    def test_create_only_and_path_containment(self):
        target = self.base / "text.vtt"
        storage.write(target, b"original", replace=False)
        with self.assertRaises(FileExistsError):
            storage.write(target, b"replacement", replace=False)
        self.assertEqual(target.read_bytes(), b"original")
        with self.assertRaises(ValueError):
            storage.inside(self.base, "../escape")
        (self.base / "link").symlink_to(self.base.parent)
        with self.assertRaises(ValueError):
            storage.inside(self.base, "link/escape")

    def test_archive_lock(self):
        with storage.archive_lock(self.cfg.archive_root):
            with self.assertRaises(ValueError):
                with storage.archive_lock(self.cfg.archive_root):
                    self.fail("Concurrent lock acquired")


class SyncTests(Fixture):
    def test_one_broken_format_does_not_block_another(self):
        ep = episode()
        ep["transcripts"].append(
            {"source_url": "https://example.com/one.json", "mime_type": "application/json"}
        )
        with (
            patch.object(podcast, "discover", return_value=([ep], {}, b"<rss/>")),
            patch.object(
                sync.network,
                "fetch",
                side_effect=[
                    OSError("unavailable"),
                    (b'[{"start":1,"end":2,"text":"Hello"}]', "https://example.com/one.json"),
                ],
            ),
        ):
            result = sync.synchronize(self.source, self.cfg)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["downloaded"], 1)
        self.assertEqual(
            len(storage.index_read(self.cfg.archive_root, self.source.id)["one"]["transcripts"]), 1
        )

    def test_first_download_then_noop(self):
        self.install_episode()
        result = self.sync([episode()])
        self.assertEqual(result["downloaded"], 0)
        self.assertEqual(result["unchanged"], 1)

    def test_local_edit_damage_and_upstream_removal_are_preserved(self):
        row, path = self.install_episode()
        for content in (b"manual edit", b""):
            path.write_bytes(content)
            result = self.sync([episode()])
            self.assertEqual(path.read_bytes(), content)
            self.assertTrue(result["warnings"])
            self.assertEqual(result["downloaded"], 0)
        self.sync([])
        self.assertTrue(path.exists())
        self.assertIn(row["episode_key"], storage.index_read(self.cfg.archive_root, self.source.id))

    def test_dry_run_is_completely_write_free(self):
        result = self.sync([episode()], dry=True)
        self.assertEqual(result["downloaded"], 1)
        self.assertFalse(self.cfg.archive_root.exists())
        self.assertFalse(self.cfg.logging_directory.exists())

    def test_fixed_boundary_and_explicit_backfill(self):
        source = replace(self.source, initial_days=30)
        recent = datetime.now(UTC) - timedelta(days=1)
        old = recent - timedelta(days=100)
        eps = [episode("recent", recent.isoformat()), episode("old", old.isoformat())]
        result = self.sync(eps, source)
        self.assertEqual(result["downloaded"], 1)
        state = storage.json_read(self.cfg.archive_root / source.id / "state.json")
        result = self.sync(eps, replace(source, initial_days=None))
        self.assertEqual(result["downloaded"], 0)
        self.assertTrue(result["warnings"])
        self.assertEqual(
            storage.json_read(self.cfg.archive_root / source.id / "state.json")["since"], state["since"]
        )
        self.assertEqual(self.sync(eps, source, backfill=365)["downloaded"], 1)
        widened = storage.json_read(self.cfg.archive_root / source.id / "state.json")["since"]
        self.sync(eps, source, backfill=2)
        self.assertEqual(
            storage.json_read(self.cfg.archive_root / source.id / "state.json")["since"], widened
        )
        self.sync(eps, source, backfill="all")
        self.assertIsNone(storage.json_read(self.cfg.archive_root / source.id / "state.json")["since"])

    def test_failed_download_retries_after_leaving_feed(self):
        result = self.sync([episode()], error=OSError("offline"))
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.sync([])["downloaded"], 1)

    def test_publisher_source_missing_or_broken_never_transcribes(self):
        local = replace(self.source, transcription="local")
        with patch.object(transcription, "generate", side_effect=AssertionError("Unexpected audio")):
            result = self.sync(
                [episode(), episode("missing", official=False)], local, error=OSError("offline")
            )
        self.assertEqual(result["local_candidates"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertFalse(result["local_transcription_enabled"])

    def test_publisher_history_survives_missing_current_links(self):
        self.install_episode()
        result = self.sync([episode("new", official=False)], replace(self.source, transcription="local"))
        self.assertTrue(result["publisher_transcript_source"])
        self.assertEqual(result["local_candidates"], 0)

    def test_unsupported_publisher_format_still_blocks_audio(self):
        data = b'<rss xmlns:p="urn:podcast"><channel><item><guid>x</guid><p:transcript url="https://example.com/t.html" type="text/html"/></item></channel></rss>'
        episodes = podcast.parse_feed(data, {"text/vtt"})
        self.assertTrue(episodes[0]["publisher_transcripts_advertised"])
        self.assertEqual(episodes[0]["transcripts"], [])
        self.assertTrue(sync.publisher_source(episodes, {}))

    def test_local_retry_after_later_success(self):
        source = replace(self.source, transcription="local")
        calls = []

        def generate(s, ep, settings):
            calls.append(ep["episode_key"])
            if ep["episode_key"] == "bad":
                raise ValueError("failed")
            return {".local.vtt": VTT}, {"audio_sha256": "fake", "origin": "locally_generated"}

        with patch.object(transcription, "generate", side_effect=generate):
            result = self.sync([episode("bad", official=False), episode("good", official=False)], source)
            self.assertEqual(result["locally_transcribed"], 1)
            calls.clear()
            self.sync([], source)
            self.assertEqual(calls, ["bad"])

    def test_unindexed_local_output_is_never_overwritten_or_retranscribed(self):
        ep = episode(official=False)
        path = (
            self.cfg.archive_root / self.source.id / "transcripts" / (transcription.stem(ep) + ".local.vtt")
        )
        storage.write(path, b"original", replace=False)
        with patch.object(transcription, "generate", side_effect=AssertionError("Unexpected audio")):
            result = self.sync([ep], replace(self.source, transcription="local"))
        self.assertEqual(path.read_bytes(), b"original")
        self.assertEqual(result["failed"], 1)

    def test_invalid_publisher_response_and_undated_episode(self):
        with (
            patch.object(podcast, "discover", return_value=([episode()], {}, b"<rss/>")),
            patch.object(
                sync.network, "fetch", return_value=(b"<html>sign in</html>", "https://example.com/login")
            ),
        ):
            result = sync.synchronize(self.source, self.cfg)
        self.assertEqual(result["failed"], 1)
        self.assertFalse(list(self.cfg.archive_root.glob("*/transcripts/*")))
        result = self.sync([episode(published="unknown")])
        self.assertEqual(result["downloaded"], 0)
        self.assertTrue(result["warnings"])


class SearchTests(Fixture):
    def test_search_phrase_across_cues_filters_and_pagination(self):
        self.install_episode()
        result = search.search(self.cfg, "RENEWABLE ENERGY")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["hits"][0]["start"], 4)
        self.assertEqual(search.search(self.cfg, "robots", since="2026-11-21")["total"], 0)
        result = search.search(self.cfg, "robots", limit=1, offset=1)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["hits"], [])

    def test_show_and_duplicate_formats(self):
        row, _ = self.install_episode()
        path = self.cfg.archive_root / self.source.id / "transcripts" / "copy.json"
        storage.write(
            path,
            json.dumps([{"start": 1, "end": 3, "text": "Guest: Local robots are useful."}]).encode(),
            replace=False,
        )
        row["transcripts"].append(
            {"local_path": str(path.relative_to(self.cfg.archive_root)), "mime_type": "application/json"}
        )
        storage.index_write(self.cfg.archive_root, self.source.id, {row["episode_key"]: row})
        self.assertEqual(search.search(self.cfg, "robots")["total"], 1)
        result = search.show(self.cfg, self.source.id, row["episode_key"], "00:00:04", "00:00:06")
        self.assertEqual(len(result["segments"]), 1)
        self.assertEqual(result["segments"][0]["start"], 4)

    def test_missing_and_malformed_files_are_reported(self):
        _, path = self.install_episode()
        path.write_text("not a transcript")
        result = search.search(self.cfg, "robots")
        self.assertEqual(result["total"], 0)
        self.assertTrue(result["warnings"])
        path.unlink()
        self.assertTrue(search.search(self.cfg, "robots")["warnings"])

    def test_json_fallback_and_disabled_source_still_searchable(self):
        row, path = self.install_episode()
        path.unlink()
        target = path.with_suffix(".json")
        target.write_text(json.dumps({"segments": [{"start": 1, "end": 2, "text": "Robots"}]}))
        row["transcripts"].append(
            {"local_path": str(target.relative_to(self.cfg.archive_root)), "mime_type": "application/json"}
        )
        storage.index_write(self.cfg.archive_root, self.source.id, {row["episode_key"]: row})
        cfg = replace(self.cfg, sources=(replace(self.source, enabled=False),))
        result = search.search(cfg, "robots")
        self.assertEqual(result["total"], 1)
        self.assertTrue(result["warnings"])

    def test_retrieval_never_writes_or_downloads(self):
        self.install_episode()
        before = {p: p.stat().st_mtime_ns for p in self.cfg.archive_root.rglob("*")}
        with patch.object(sync.network, "fetch", side_effect=AssertionError("Unexpected network")):
            search.search(self.cfg, "robots")
            search.show(self.cfg, self.source.id, "one")
            search.records(self.cfg)
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in self.cfg.archive_root.rglob("*")})
        self.assertFalse(self.cfg.logging_directory.exists())


class ReportAndCLITests(Fixture):
    def test_report_lifecycle_and_redaction(self):
        report = reports.Report(self.cfg, "run")
        self.assertIn("completion", reports.read(self.cfg.logging_directory, "latest"))
        report.event("error", "test", message="https://name:secret@example.com/file?token=hidden#part")
        report.finish("success")
        result = reports.read(self.cfg.logging_directory, "latest")
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(reports.list_runs(self.cfg.logging_directory, 10)), 1)
        content = (report.path / "events.jsonl").read_text()
        self.assertNotIn("secret", content)
        self.assertNotIn("hidden", content)
        self.assertEqual(
            [json.loads(line)["event"] for line in content.splitlines()],
            ["run_started", "test", "run_finished"],
        )

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--config", str(self.file), "--json", *args])
        return code, json.loads(out.getvalue())

    def test_global_options_before_and_after_commands(self):
        for args in (
            ["--json", "--config", str(self.file), "config", "check"],
            ["config", "check", "--json", "--config", str(self.file)],
        ):
            self.assertTrue(vars(cli.parser().parse_args(args))["json"])
        self.assertEqual(self.invoke("config", "check")[0], 0)

    def test_cli_run_report_and_status(self):
        with (
            patch.object(podcast, "discover", return_value=([episode()], {}, b"<rss/>")),
            patch.object(sync.network, "fetch", return_value=(VTT, "https://example.com/t.vtt")),
        ):
            code, result = self.invoke("run")
        self.assertEqual(code, 0)
        self.assertEqual(result["totals"]["downloaded"], 1)
        self.assertEqual(self.invoke("runs", "show", "latest")[1]["run_id"], result["run_id"])
        self.assertEqual(self.invoke("search", "robots")[1]["total"], 1)

    def test_cli_dry_run_no_log_and_error_codes(self):
        with patch.object(podcast, "discover", return_value=([episode()], {}, b"<rss/>")):
            self.assertEqual(self.invoke("run", "--dry-run")[0], 0)
        self.assertFalse(self.cfg.archive_root.exists())
        self.assertFalse(self.cfg.logging_directory.exists())
        self.assertEqual(self.invoke("run", "--source", "unknown")[0], 2)
        self.file.write_text("version: 2")
        self.assertEqual(self.invoke("config", "check")[0], 2)


class InstagramTests(Fixture):
    def embed(self, *nodes):
        return ("<script>" + json.dumps({"posts": list(nodes)}) + "</script>").encode()

    def node(self, code, day=20, caption="Idée of the Day: Test", video=True, owner="example_account"):
        return {
            "shortcode": code,
            "owner": {"username": owner},
            "is_video": video,
            "taken_at_timestamp": datetime(2026, 11, day, tzinfo=UTC).timestamp(),
            "edge_media_to_caption": {"edges": [{"node": {"text": caption}}]},
        }

    def test_filter_accents_photos_carousels_and_owner(self):
        source = {"username": "example_account", "caption_contains": "idee of the day"}
        carousel = {**self.node("carousel"), "__typename": "GraphSidecar"}
        episodes, discovery = instagram.parse_profile(
            self.embed(
                self.node("keep"),
                self.node("photo", video=False),
                self.node("ad", caption="Not this series"),
                carousel,
            ),
            source,
        )
        self.assertEqual([e["episode_number"] for e in episodes], ["keep"])
        self.assertEqual(discovery["visible_posts"], 4)
        with self.assertRaises(ValueError):
            instagram.parse_profile(self.embed(self.node("bad", owner="someone_else")), source)
        with self.assertRaises(ValueError):
            instagram.parse_profile(b"<html>login</html>", source)

    def test_persistent_gap_and_retry_after_window(self):
        source = replace(
            self.source,
            type="instagram",
            username="example_account",
            caption_contains="series",
            transcription="local",
        )
        old, new = (
            episode("old", "2026-11-01T00:00:00+00:00", False),
            episode("new", "2026-11-20T00:00:00+00:00", False),
        )
        calls = []

        def generate(s, ep, settings):
            calls.append(ep["episode_key"])
            raise ValueError("temporary failure")

        def run(eps, first, last):
            with (
                patch.object(
                    instagram,
                    "discover",
                    return_value=(eps, {"oldest_visible": first, "latest_visible": last}, None),
                ),
                patch.object(transcription, "generate", side_effect=generate),
            ):
                return sync.synchronize(source, self.cfg)

        run([old], old["published"], old["published"])
        calls.clear()
        result = run([new], new["published"], new["published"])
        self.assertEqual(set(calls), {"old", "new"})
        self.assertTrue(result["warnings"])
        self.assertTrue(run([new], new["published"], new["published"])["warnings"])

    def test_download_ignores_configuration_and_cookies(self):
        def download(command, **kwargs):
            self.assertIn("--ignore-config", command)
            self.assertNotIn("--cookies-from-browser", command)
            (self.base / "media.m4a").write_bytes(b"audio")

        with patch.object(instagram.subprocess, "run", side_effect=download):
            self.assertEqual(
                instagram.download_audio("https://example.com/video", self.base, {}), self.base / "media.m4a"
            )


class TranscriptionTests(Fixture):
    def generated(self, invalid_first=False, invalid_retry=False, empty=False):
        calls, audio_paths = [], []
        settings = {**self.cfg.transcription, "python": str(Path("/bin/sh"))}

        def worker(command, **kwargs):
            calls.append(command)
            audio_paths.append(Path(command[2]))
            result = {
                "text": "" if empty else "Unchanged generated words.",
                "audio_duration": 5,
                "segments": [{"start": 1, "end": 3, "text": "Unchanged generated words."}],
            }
            if invalid_retry or invalid_first and len(calls) == 1:
                result["segments"][0]["end"] = 0
            Path(command[3]).write_text(json.dumps(result))

        def download(url, path, limit):
            path.write_bytes(b"fake audio")

        with (
            patch.object(transcription.network, "download", side_effect=download),
            patch.object(transcription.subprocess, "run", side_effect=worker),
        ):
            try:
                output = transcription.generate(self.source, episode(official=False), settings)
            finally:
                self.assertTrue(all(not p.exists() for p in audio_paths))
        return output, calls

    def test_first_result_valid_and_unchanged(self):
        (payloads, provenance), calls = self.generated()
        self.assertEqual(len(calls), 1)
        self.assertIn(b"Unchanged generated words.", payloads[".local.vtt"])
        self.assertNotIn("timestamp_retry", provenance)

    def test_retry_options_and_provenance(self):
        (payloads, provenance), calls = self.generated(invalid_first=True)
        self.assertEqual(len(calls), 2)
        self.assertIn("--align-words", calls[-1])
        self.assertEqual(provenance["timestamp_retry"], "word_alignment_without_previous_text")
        self.assertIn(b"Unchanged generated words.", payloads[".local.md"])

    def test_second_failure_and_no_speech_never_write_archive(self):
        for kwargs in ({"invalid_retry": True}, {"empty": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.generated(**kwargs)
        self.assertFalse(self.cfg.archive_root.exists())


if __name__ == "__main__":
    unittest.main()
