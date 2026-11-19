"""Offline tests for the standalone distribution-config downloader."""

from contextlib import redirect_stderr
from datetime import datetime
import hashlib
from http.client import IncompleteRead
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from urllib import error, request
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / "overlay" / "sync_config.py"
SPEC = importlib.util.spec_from_file_location("sync_config", SCRIPT)
sync_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_config)
CONFIG = b"[General]\r\nEnabled=1\r\nPercent=100%\r\n"
SHA = "a" * 40


def make_zip(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return stream.getvalue()


class ConfigTests(unittest.TestCase):
    def test_limits(self):
        self.assertEqual(sync_config.MAX_DOWNLOAD_BYTES, 32 * 1024 * 1024)
        self.assertEqual(sync_config.MAX_CONFIG_BYTES, 2 * 1024 * 1024)

    def test_plain_ini_is_returned_verbatim(self):
        self.assertEqual(sync_config.config_bytes(CONFIG), CONFIG)

    def test_utf8_bom_and_gbk_are_preserved(self):
        for data in (b"\xef\xbb\xbf" + "[General]\nName=中文\n".encode(),
                     "[General]\nName=中文\n".encode("gbk")):
            with self.subTest(data=data):
                self.assertEqual(sync_config.config_bytes(data), data)

    def test_root_zip_config_wins_over_nested(self):
        archive = make_zip([("config.ini", CONFIG), ("nested/config.ini", b"invalid")])
        self.assertEqual(sync_config.config_bytes(archive), CONFIG)

    def test_unique_nested_zip_config(self):
        for name in ("release/config.ini", "release/CONFIG.INI", "release\\config.ini"):
            with self.subTest(name=name):
                archive = make_zip([("readme.txt", b"ignored"), (name, CONFIG)])
                self.assertEqual(sync_config.config_bytes(archive), CONFIG)

    def test_zip_is_never_extracted(self):
        archive = make_zip([("../../config.ini", CONFIG)])
        with mock.patch.object(zipfile.ZipFile, "extract", side_effect=AssertionError), \
             mock.patch.object(zipfile.ZipFile, "extractall", side_effect=AssertionError):
            self.assertEqual(sync_config.config_bytes(archive), CONFIG)

    def test_invalid_ini(self):
        for data in (b"", b"; comment only", b"key=value", b"<html>Error</html>",
                     b"[DEFAULT]\nkey=value", b"[General]\nkey=1\nkey=2\n",
                     b"[General]\n[General]\n", b"[General]\ninvalid line\n",
                     b"[General]\nkey=\x00\n", b"[General]\nkey=\xff\n"):
            with self.subTest(data=data), self.assertRaises(sync_config.SyncError):
                sync_config.config_bytes(data)

    def test_zip_ambiguity_and_missing_config(self):
        cases = [[], [("readme.txt", CONFIG)],
                 [("a/config.ini", CONFIG), ("b/config.ini", CONFIG)],
                 [("config.ini", CONFIG), ("CONFIG.INI", CONFIG)],
                 [("config.ini/", b"")]]
        for entries in cases:
            with self.subTest(entries=entries), self.assertRaises(sync_config.SyncError):
                sync_config.config_bytes(make_zip(entries))

    def test_duplicate_identical_zip_names_are_ambiguous(self):
        with mock.patch("warnings.warn"):
            archive = make_zip([("config.ini", CONFIG), ("config.ini", CONFIG)])
        with self.assertRaises(sync_config.SyncError):
            sync_config.config_bytes(archive)

    def test_zip_with_invalid_ini_is_rejected(self):
        with self.assertRaises(sync_config.SyncError):
            sync_config.config_bytes(make_zip([("config.ini", b"invalid")]))

    def test_corrupt_compressed_zip_is_rejected(self):
        archive = bytearray(make_zip([("config.ini", CONFIG)]))
        with zipfile.ZipFile(io.BytesIO(archive)) as reader:
            entry = reader.getinfo("config.ini")
            start = entry.header_offset + 30 + len(entry.filename.encode()) + len(entry.extra)
            archive[start:start + entry.compress_size] = b"\xff" * entry.compress_size
        with self.assertRaises(sync_config.SyncError):
            sync_config.config_bytes(bytes(archive))

    def test_plain_and_compressed_config_size_caps(self):
        data = b"[General]\nkey=" + b"x" * sync_config.MAX_CONFIG_BYTES
        for payload in (data, make_zip([("config.ini", data)])):
            with self.subTest(zipped=payload is not data), self.assertRaises(sync_config.SyncError):
                sync_config.config_bytes(payload)

    def test_config_at_size_cap_is_accepted(self):
        with mock.patch.object(sync_config, "MAX_CONFIG_BYTES", len(CONFIG)):
            self.assertEqual(sync_config.config_bytes(CONFIG), CONFIG)
            self.assertEqual(sync_config.config_bytes(make_zip([("config.ini", CONFIG)])), CONFIG)

    def test_limited_read_handles_short_reads_and_boundary(self):
        class ShortReader(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 3))
        self.assertEqual(sync_config.read_limited(ShortReader(b"abcde"), 5), b"abcde")
        with self.assertRaises(sync_config.SyncError):
            sync_config.read_limited(ShortReader(b"abcdef"), 5)


class FetchTests(unittest.TestCase):
    def test_default_sources_are_direct_and_ordered(self):
        self.assertEqual(sync_config.DEFAULT_SOURCES, (
            "https://raw.githubusercontent.com/CodeCubist/FufuLauncher--Plugins/main/FuFuPlugin.zip",
            "https://raw.githubusercontent.com/FufuLauncher/FufuLauncher--Plugins/main/FuFuPlugin.zip",
        ))

    def test_github_branch_resolves_to_immutable_raw_download(self):
        for source in (sync_config.DEFAULT_SOURCES[0],
                       "https://github.com/CodeCubist/FufuLauncher--Plugins/blob/main/FuFuPlugin.zip",
                       "https://github.com/CodeCubist/FufuLauncher--Plugins/raw/main/FuFuPlugin.zip"):
            with self.subTest(source=source):
                immutable = ("https://raw.githubusercontent.com/CodeCubist/"
                             f"FufuLauncher--Plugins/{SHA}/FuFuPlugin.zip")
                with mock.patch.object(sync_config, "download", side_effect=[
                    (json.dumps({"sha": SHA}).encode(), "unused"),
                    (make_zip([("config.ini", CONFIG)]), immutable),
                ]) as download:
                    data, metadata = sync_config.fetch_config([source])
                self.assertEqual(data, CONFIG)
                self.assertEqual(metadata, {"source_url": source, "resolved_url": immutable,
                                           "repository": "CodeCubist/FufuLauncher--Plugins",
                                           "revision": SHA})
                self.assertEqual(download.call_args_list, [
                    mock.call("https://api.github.com/repos/CodeCubist/"
                              "FufuLauncher--Plugins/commits/main", api=True),
                    mock.call(immutable),
                ])

    def test_pinned_github_url_does_not_need_api(self):
        url = f"https://raw.githubusercontent.com/owner/repo/{SHA}/FuFuPlugin.zip"
        with mock.patch.object(sync_config, "download", return_value=(CONFIG, url)) as download:
            _, metadata = sync_config.fetch_config([url])
        download.assert_called_once_with(url)
        self.assertEqual(metadata["revision"], SHA)

    def test_api_failure_or_invalid_response_falls_back_without_revision(self):
        source = sync_config.DEFAULT_SOURCES[0]
        for response in (error.URLError("offline"), (b"not json", "unused"),
                         (b'{"sha":"not-a-sha"}', "unused"), (b"[]", "unused")):
            with self.subTest(response=response):
                with mock.patch.object(sync_config, "download", side_effect=[
                    response, (CONFIG, source),
                ]) as download:
                    _, metadata = sync_config.fetch_config([source])
                self.assertNotIn("revision", metadata)
                self.assertEqual(metadata["resolved_url"], source)
                self.assertEqual(download.call_args_list[-1], mock.call(source))

    def test_sources_tried_in_order_until_valid(self):
        sources = ["https://example.com/first", "https://example.com/second",
                   "https://example.com/third", "https://example.com/unused"]
        with mock.patch.object(sync_config, "download", side_effect=[
            error.URLError("offline"), (b"invalid", sources[1]), (CONFIG, sources[2]),
        ]) as download:
            data, metadata = sync_config.fetch_config(sources)
        self.assertEqual(data, CONFIG)
        self.assertEqual(metadata["source_url"], sources[2])
        self.assertIsNone(metadata["repository"])
        self.assertEqual(download.call_args_list, [mock.call(url) for url in sources[:3]])

    def test_redirect_destination_is_recorded(self):
        with mock.patch.object(sync_config, "download", return_value=(CONFIG, "https://cdn.example/config.ini")):
            _, metadata = sync_config.fetch_config(["https://example.com/config.ini"])
        self.assertEqual(metadata["resolved_url"], "https://cdn.example/config.ini")

    def test_unresolved_github_source_does_not_invent_repository(self):
        self.assertIsNone(sync_config.github_source("https://example.com/owner/repo/main/file.zip"))


class TransportTests(unittest.TestCase):
    def response(self, data, url):
        result = io.BytesIO(data)
        result.geturl = lambda: url
        return result

    def test_token_only_sent_to_api_not_downloads(self):
        cases = [("https://api.github.com/repos/owner/repo/commits/main", True, True),
                 ("https://api.github.com/other", False, False),
                 ("https://raw.githubusercontent.com/owner/repo/main/file.zip", False, False),
                 ("https://github.com/owner/repo/raw/main/file.zip", False, False),
                 ("https://example.com/file.zip", True, False),
                 ("https://api.github.com.evil.example/file.zip", True, False)]
        for url, api, authorized in cases:
            with self.subTest(url=url, api=api), \
                 mock.patch.dict(os.environ, {"GH_TOKEN": "test-secret"}), \
                 mock.patch.object(sync_config.request, "build_opener") as build:
                build.return_value.open.return_value = self.response(CONFIG, url)
                self.assertEqual(sync_config.download(url, api=api), (CONFIG, url))
                req = build.return_value.open.call_args.args[0]
                self.assertEqual(req.get_header("Authorization"),
                                 "Bearer test-secret" if authorized else None)
                self.assertEqual(build.return_value.open.call_args.kwargs["timeout"],
                                 sync_config.TIMEOUT_SECONDS)

    def test_no_token_is_required(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(sync_config.request, "build_opener") as build:
            url = "https://api.github.com/repos/owner/repo/commits/main"
            build.return_value.open.return_value = self.response(b"{}", url)
            sync_config.download(url, api=True)
        self.assertIsNone(build.return_value.open.call_args.args[0].get_header("Authorization"))

    def test_api_redirect_strips_authorization(self):
        req = request.Request("https://api.github.com/repos/owner/repo/commits/main",
                              headers={"Authorization": "Bearer test-secret"})
        handler = sync_config.SafeRedirectHandler()
        for target in ("https://raw.githubusercontent.com/owner/repo/main/file.zip",
                       "https://third-party.example/file.zip", "https://api.github.com/elsewhere"):
            with self.subTest(target=target):
                redirected = handler.redirect_request(req, None, 302, "Found", {}, target)
                self.assertIsNone(redirected.get_header("Authorization"))
        with self.assertRaises(sync_config.SyncError):
            handler.redirect_request(req, None, 302, "Found", {}, "http://example.com/file")

    def test_download_size_cap(self):
        url = "https://example.com/file.zip"
        with mock.patch.object(sync_config, "MAX_DOWNLOAD_BYTES", 5), \
             mock.patch.object(sync_config.request, "build_opener") as build:
            build.return_value.open.return_value = self.response(b"123456", url)
            with self.assertRaises(sync_config.SyncError):
                sync_config.download(url)

    def test_incomplete_http_response_is_a_source_failure(self):
        with mock.patch.object(sync_config.request, "build_opener") as build:
            build.return_value.open.side_effect = IncompleteRead(b"partial", 100)
            with self.assertRaises(sync_config.SyncError):
                sync_config.download("https://example.com/file.zip")

    def test_insecure_or_credentialed_sources_are_rejected(self):
        for url in ("http://example.com/config.ini", "file:///tmp/config.ini",
                    "https://name:secret@example.com/config.ini"):
            with self.subTest(url=url), self.assertRaises(sync_config.SyncError):
                sync_config.download(url)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "output.ini"
        self.metadata = self.root / "source.json"
        self.template = self.root / "template.ini"
        self.template.write_bytes(CONFIG)

    def test_plain_and_zip_templates_offline_digest_and_provenance(self):
        for payload in (CONFIG, make_zip([("release/config.ini", CONFIG)])):
            with self.subTest(zipped=payload is not CONFIG):
                self.template.write_bytes(payload)
                with mock.patch.object(sync_config, "download", side_effect=AssertionError("network")):
                    result = sync_config.sync(self.output, self.metadata, template=self.template)
                self.assertEqual(self.output.read_bytes(), CONFIG)
                metadata = json.loads(self.metadata.read_bytes())
                self.assertEqual(result, metadata)
                self.assertEqual(metadata["sha256"], hashlib.sha256(CONFIG).hexdigest())
                self.assertEqual(metadata["source_url"], self.template.as_uri())
                self.assertEqual(metadata["resolved_url"], self.template.as_uri())
                self.assertIsNone(metadata["repository"])
                self.assertNotIn("revision", metadata)
                self.assertIsNotNone(datetime.fromisoformat(metadata["fetched_at"]).utcoffset())

    def test_network_metadata_hashes_config_not_zip(self):
        url = sync_config.DEFAULT_SOURCES[0]
        resolved = url.replace("/main/", f"/{SHA}/")
        archive = make_zip([("config.ini", CONFIG)])
        with mock.patch.object(sync_config, "download", side_effect=[
            (json.dumps({"sha": SHA}).encode(), "unused"), (archive, resolved),
        ]):
            sync_config.sync(self.output, self.metadata)
        metadata = json.loads(self.metadata.read_bytes())
        self.assertEqual(metadata["revision"], SHA)
        self.assertEqual(metadata["repository"], "CodeCubist/FufuLauncher--Plugins")
        self.assertEqual(metadata["sha256"], hashlib.sha256(CONFIG).hexdigest())
        self.assertNotEqual(metadata["sha256"], hashlib.sha256(archive).hexdigest())

    def test_success_replaces_without_merging_or_backups(self):
        self.output.write_bytes(b"[Local]\nOnlyLocal=1\n")
        sync_config.sync(self.output, self.metadata, template=self.template)
        self.assertEqual(self.output.read_bytes(), CONFIG)
        self.assertEqual({p.name for p in self.root.iterdir()},
                         {self.output.name, self.metadata.name, self.template.name})

    def assert_failure_preserves_files(self, operation):
        self.output.write_bytes(b"existing config")
        self.metadata.write_bytes(b"existing metadata")
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (self.output, self.metadata)]
        with self.assertRaises((sync_config.SyncError, OSError)):
            operation()
        after = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (self.output, self.metadata)]
        self.assertEqual(before, after)
        self.assertFalse(list(self.root.glob(".*.tmp")))

    def test_all_sources_fail_without_touching_output(self):
        with mock.patch.object(sync_config, "download", side_effect=error.URLError("offline")):
            self.assert_failure_preserves_files(
                lambda: sync_config.sync(self.output, self.metadata))

    def test_invalid_ambiguous_oversize_and_missing_templates_leave_output_untouched(self):
        for payload in (b"invalid", make_zip([("a/config.ini", CONFIG), ("b/config.ini", CONFIG)]),
                        b"x" * (sync_config.MAX_CONFIG_BYTES + 1)):
            with self.subTest(size=len(payload)):
                self.template.write_bytes(payload)
                self.assert_failure_preserves_files(
                    lambda: sync_config.sync(self.output, self.metadata, template=self.template))
        self.template.unlink()
        self.assert_failure_preserves_files(
            lambda: sync_config.sync(self.output, self.metadata, template=self.template))

    def test_template_download_size_cap(self):
        with mock.patch.object(sync_config, "MAX_DOWNLOAD_BYTES", len(CONFIG) - 1):
            self.assert_failure_preserves_files(
                lambda: sync_config.sync(self.output, self.metadata, template=self.template))

    def test_metadata_staging_failure_leaves_config_untouched(self):
        real_stage = sync_config.stage_file
        def fail_metadata(path, data):
            if path == self.metadata:
                raise OSError("metadata unavailable")
            return real_stage(path, data)
        with mock.patch.object(sync_config, "stage_file", side_effect=fail_metadata):
            self.assert_failure_preserves_files(
                lambda: sync_config.sync(self.output, self.metadata, template=self.template))

    def test_metadata_publication_failure_leaves_config_untouched(self):
        with mock.patch.object(sync_config.os, "replace", side_effect=OSError("read only")):
            self.assert_failure_preserves_files(
                lambda: sync_config.sync(self.output, self.metadata, template=self.template))

    def test_output_is_published_using_atomic_replace(self):
        real_replace = os.replace
        replacements = []
        def replace(source, destination):
            self.assertEqual(Path(source).parent, Path(destination).parent)
            replacements.append(Path(destination))
            real_replace(source, destination)
        with mock.patch.object(sync_config.os, "replace", side_effect=replace):
            sync_config.sync(self.output, self.metadata, template=self.template)
        self.assertEqual(replacements, [self.metadata, self.output])

    def test_same_output_and_metadata_is_rejected_without_touching(self):
        self.output.write_bytes(CONFIG)
        with self.assertRaises(sync_config.SyncError):
            sync_config.sync(self.output, self.output, template=self.template)
        self.assertEqual(self.output.read_bytes(), CONFIG)

    def test_failure_does_not_create_output(self):
        self.template.write_bytes(b"invalid")
        with self.assertRaises(sync_config.SyncError):
            sync_config.sync(self.output, self.metadata, template=self.template)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.metadata.exists())

    def test_cli_repeatable_sources_replace_defaults(self):
        with mock.patch.object(sync_config, "sync") as sync:
            status = sync_config.main(["--output", str(self.output), "--metadata", str(self.metadata),
                                       "--source", "https://example.com/one", "--source", "https://example.com/two"])
        self.assertEqual(status, 0)
        self.assertEqual(sync.call_args.args[2], ["https://example.com/one", "https://example.com/two"])

    def test_cli_failure_is_nonzero_and_actionable(self):
        self.template.write_bytes(b"invalid")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = sync_config.main(["--output", str(self.output), "--metadata", str(self.metadata),
                                       "--template", str(self.template)])
        self.assertEqual(status, 1)
        self.assertIn("invalid config.ini", stderr.getvalue())
        self.assertFalse(self.output.exists())

    def test_copied_script_runs_standalone_in_worktree(self):
        copied = self.root / "sync_config.py"
        shutil.copyfile(SCRIPT, copied)
        result = subprocess.run([sys.executable, str(copied), "--output", str(self.output),
                                 "--metadata", str(self.metadata), "--template", str(self.template)],
                                cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_bytes(), CONFIG)
        self.assertEqual(json.loads(self.metadata.read_bytes())["sha256"],
                         hashlib.sha256(CONFIG).hexdigest())


if __name__ == "__main__":
    unittest.main()
