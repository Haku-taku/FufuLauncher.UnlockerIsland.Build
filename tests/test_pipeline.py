"""Offline pipeline tests: no compiler, Git checkout, gh credentials, or network."""

import argparse
import base64
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pipeline.py"
SPEC = importlib.util.spec_from_file_location("pipeline_under_test", SCRIPT)
pipeline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pipeline)
WRAPPER = "a" * 40
PINNED = "b" * 40
TARGET = "c" * 40
INPUTS = "d" * 64
REPOSITORY = "owner/build"


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name, path in (("ROOT", self.root), ("UPSTREAM", self.root / "upstream"),
                           ("SOURCE", self.root / ".work/source"), ("DIST", self.root / "dist")):
            self.stack.enter_context(mock.patch.object(pipeline, name, path))
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "2", "FORCE_BUILD": "false"}, clear=True))
        # Every external process must be explicitly mocked by the individual test.
        self.process = self.stack.enter_context(mock.patch.object(
            pipeline.subprocess, "run", side_effect=AssertionError("unexpected subprocess")))
        self.stack.enter_context(mock.patch.object(
            pipeline.subprocess, "Popen", side_effect=AssertionError("unexpected build")))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(redirect_stderr(io.StringIO()))

    def write(self, relative, content=b"fixture\n"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def patch(self, name, **kwargs):
        return self.stack.enter_context(mock.patch.object(pipeline, name, **kwargs))


class FingerprintTests(PipelineTestCase):
    def test_ignores_submodule_docs_dist_tests_and_bytecode(self):
        self.write("scripts/pipeline.py")
        before = pipeline.fingerprint()
        for name in ("upstream/build.sh", "README.md", "docs/guide.md", "LICENSE",
                     "dist/update.json", "tests/test_pipeline.py", ".gitmodules",
                     "scripts/__pycache__/pipeline.cpython-313.pyc"):
            self.write(name, b"not a recipe")
        self.assertEqual(pipeline.fingerprint(), before)

    def test_includes_recipe_contents_and_paths(self):
        for name in ("patches/0001.patch", "overlay/build.sh", "scripts/pipeline.py",
                     ".github/workflows/build.yml"):
            with self.subTest(name=name):
                before = pipeline.fingerprint()
                path = self.write(name, b"recipe v1")
                added = pipeline.fingerprint()
                self.assertNotEqual(before, added)
                path.write_bytes(b"recipe v2")
                changed = pipeline.fingerprint()
                self.assertNotEqual(added, changed)
                path.rename(path.with_name("renamed-" + path.name))
                self.assertNotEqual(changed, pipeline.fingerprint())

    def test_fingerprint_does_not_depend_on_creation_order(self):
        first = self.write("patches/z.patch", b"z")
        second = self.write("patches/a.patch", b"a")
        before = pipeline.fingerprint()
        first.unlink()
        second.unlink()
        self.write("patches/a.patch", b"a")
        self.write("patches/z.patch", b"z")
        self.assertEqual(pipeline.fingerprint(), before)


class PrepareTests(PipelineTestCase):
    def test_valid_sha_requires_exact_lowercase_hex(self):
        self.assertEqual(pipeline.valid_sha(TARGET), TARGET)
        for value in (None, 123, "", "HEAD", "--help", "../source", "C" * 40,
                      "g" * 40, "c" * 39, "c" * 41, TARGET + "\n", " " + TARGET):
            with self.subTest(value=value), self.assertRaises(ValueError):
                pipeline.valid_sha(value)

    def test_invalid_sha_is_rejected_before_git_or_filesystem_changes(self):
        git = self.patch("git")
        with self.assertRaises(ValueError):
            pipeline.prepare("--upload-pack=evil")
        git.assert_not_called()
        self.assertFalse(pipeline.SOURCE.exists())

    def test_refuses_unregistered_existing_source(self):
        sentinel = self.write(".work/source/precious.txt", b"user work")
        git = self.patch("git", side_effect=["", f"worktree {self.root / 'another'}\nHEAD {PINNED}\n"])
        with self.assertRaisesRegex(RuntimeError, "unregistered"):
            pipeline.prepare(TARGET)
        self.assertEqual(sentinel.read_bytes(), b"user work")
        self.assertFalse(any(call.args[:2] == ("worktree", "remove") for call in git.call_args_list))

    def test_registered_worktree_recreated_then_sorted_patches_and_overlay(self):
        pipeline.SOURCE.mkdir(parents=True)
        self.write("patches/02.patch")
        self.write("patches/01.patch")
        self.write("overlay/nested/config.txt", b"overlay data")
        self.write("overlay/__pycache__/ignored.pyc")
        git = self.patch("git", return_value="")
        git.side_effect = lambda *args, **kwargs: (
            f"worktree {pipeline.SOURCE}\nHEAD {PINNED}\n" if args[:2] == ("worktree", "list") else "")
        self.assertEqual(pipeline.prepare(TARGET), pipeline.SOURCE)
        git.assert_any_call("cat-file", "-e", TARGET + "^{commit}", cwd=pipeline.UPSTREAM)
        git.assert_any_call("worktree", "remove", "--force", str(pipeline.SOURCE), cwd=pipeline.UPSTREAM)
        git.assert_any_call("worktree", "add", "--detach", str(pipeline.SOURCE), TARGET, cwd=pipeline.UPSTREAM)
        applies = [call.args[-1] for call in git.call_args_list if call.args[0] == "apply"]
        self.assertEqual(applies, [str(self.root / "patches/01.patch"), str(self.root / "patches/02.patch")])
        self.assertEqual((pipeline.SOURCE / "nested/config.txt").read_bytes(), b"overlay data")
        self.assertFalse((pipeline.SOURCE / "__pycache__").exists())

    def test_overlay_does_not_follow_upstream_symlink_outside_worktree(self):
        outside = self.write("outside-user-file", b"user work")
        self.write("patches/01.patch")
        self.write("overlay/build.sh", b"trusted overlay recipe")
        def git(*args, **kwargs):
            if args[:2] == ("worktree", "add"):
                pipeline.SOURCE.mkdir()
                (pipeline.SOURCE / "build.sh").symlink_to(outside)
            return ""
        self.patch("git", side_effect=git)
        try:
            pipeline.prepare(TARGET)
        except RuntimeError:
            # Rejecting unsafe paths or replacing the symlink are both safe.
            pass
        self.assertEqual(outside.read_bytes(), b"user work",
                         "overlay must not overwrite a symlink target outside SOURCE")

    def test_empty_patch_queue_fails_before_overlay(self):
        self.patch("git", return_value="")
        self.write("overlay/should-not-copy.txt")
        with self.assertRaisesRegex(RuntimeError, "patch queue is empty"):
            pipeline.prepare(TARGET)
        self.assertFalse((pipeline.SOURCE / "should-not-copy.txt").exists())


class StatusApiTests(PipelineTestCase):
    def test_initial_status_retains_release_not_previous_failure(self):
        previous = {"latest_release": {"version": "previous"},
                    "build": {"state": "failed", "error": "old error"}}
        before = copy.deepcopy(previous)
        self.patch("git", return_value=PINNED)
        status = pipeline.initial_status(WRAPPER, previous)
        self.assertEqual(previous, before)
        self.assertEqual(status["latest_release"], previous["latest_release"])
        self.assertEqual(status["build"]["state"], "building")
        self.assertIsNone(status["build"]["error"])
        self.assertEqual(status["build"]["attempt_id"], "123.2")
        self.assertEqual(status["upstream"]["pinned_sha"], PINNED)

    def test_load_status_decodes_contents_api_and_handles_first_run(self):
        expected = {"latest_release": {"version": "release"}}
        api = self.patch("gh_api", side_effect=[None, {"content": base64.b64encode(json.dumps(expected).encode()).decode()}])
        self.assertEqual(pipeline.load_status(REPOSITORY), {})
        self.assertEqual(pipeline.load_status(REPOSITORY), expected)
        api.assert_called_with(REPOSITORY, "contents/status.json?ref=status", missing_ok=True)

    def test_save_status_creates_branch_and_uses_contents_sha_on_update(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                status = {"build": {"state": "failed", "stage": "compile"}, "latest_release": {"version": "old"}}
                responses = ([{"object": {"sha": WRAPPER}}, {"sha": "blob-sha"}, {}]
                             if existing else [None, {}, None, {}])
                with mock.patch.object(pipeline, "gh_api", side_effect=responses) as api:
                    pipeline.save_status(REPOSITORY, status, WRAPPER)
                put = api.call_args
                self.assertEqual(put.args, (REPOSITORY, "contents/status.json"))
                self.assertEqual(put.kwargs["method"], "PUT")
                body = put.kwargs["body"]
                self.assertEqual(body["branch"], "status")
                self.assertEqual("sha" in body, existing)
                if existing:
                    self.assertEqual(body["sha"], "blob-sha")
                else:
                    api.assert_any_call(REPOSITORY, "git/refs", method="POST", body={"ref": "refs/heads/status", "sha": WRAPPER})
                self.assertEqual(json.loads(base64.b64decode(body["content"])), status)
                self.assertEqual(json.loads((pipeline.DIST / "status.json").read_text()), status)

    def test_gh_api_sends_json_on_stdin_not_shell(self):
        self.process.side_effect = None
        self.process.return_value = subprocess.CompletedProcess([], 0, '{"id": 1}', "")
        body = {"name": "quotes ' and $(not-a-command)"}
        self.assertEqual(pipeline.gh_api(REPOSITORY, "releases", method="POST", body=body), {"id": 1})
        args, = self.process.call_args.args
        self.assertEqual(args[:5], ["gh", "api", "--method", "POST", "repos/owner/build/releases"])
        self.assertEqual(json.loads(self.process.call_args.kwargs["input"]), body)
        self.assertNotIn("shell", self.process.call_args.kwargs)

    def test_only_explicit_missing_404_is_suppressed(self):
        self.process.side_effect = None
        self.process.return_value = subprocess.CompletedProcess([], 1, "", "HTTP 404: Not Found")
        self.assertIsNone(pipeline.gh_api(REPOSITORY, "contents/status.json", missing_ok=True))
        with self.assertRaises(RuntimeError):
            pipeline.gh_api(REPOSITORY, "contents/status.json")
        self.process.return_value.stderr = "HTTP 403: Forbidden"
        with self.assertRaises(RuntimeError):
            pipeline.gh_api(REPOSITORY, "contents/status.json", missing_ok=True)


class RunTests(PipelineTestCase):
    def setUp(self):
        super().setUp()
        self.previous_release = {"version": "previous", "upstream": {"sha": TARGET},
                                 "wrapper": {"inputs_sha256": INPUTS}}
        self.previous = {"latest_release": self.previous_release,
                         "build": {"state": "failed", "stage": "compile", "error": "old failure"}}
        self.git = self.patch("git", side_effect=self.git_response)
        self.load = self.patch("load_status", return_value=self.previous)
        self.saved = []
        self.save = self.patch("save_status", side_effect=lambda repo, status, sha: self.saved.append(copy.deepcopy(status)))
        self.patch("fingerprint", return_value=INPUTS)
        self.prepare = self.patch("prepare")
        self.stream = self.patch("stream")
        self.manifest = {"version": "new-release"}
        self.package = self.patch("package", return_value=(self.manifest, [pipeline.DIST / "asset.zip"]))
        self.publish = self.patch("publish")
        self.lock = self.patch("update_lock", return_value=True)
        self.args = argparse.Namespace(local=False, sha=None, template=None, force=False)

    def git_response(self, *args, **kwargs):
        if args == ("rev-parse", "HEAD"):
            return WRAPPER
        if args == ("rev-parse", "HEAD:upstream"):
            return PINNED
        if args[:1] == ("ls-remote",):
            return TARGET + "\trefs/heads/master"
        if args[:1] == ("fetch",):
            return ""
        raise AssertionError(f"unexpected git call: {args}")

    def assert_not_built(self):
        for operation in (self.prepare, self.stream, self.package, self.publish):
            operation.assert_not_called()
        self.assertFalse(any(call.args[:1] == ("fetch",) for call in self.git.call_args_list))

    def test_failed_compile_retains_previous_success_and_does_not_publish_or_lock(self):
        self.args.force = True
        self.stream.side_effect = RuntimeError("compiler failed")
        self.assertEqual(pipeline.run(self.args), 1)
        status = json.loads((pipeline.DIST / "status.json").read_text())
        self.assertEqual(status["latest_release"], self.previous_release)
        self.assertEqual(status["build"]["state"], "failed")
        self.assertEqual(status["build"]["stage"], "compile")
        self.assertIn("compiler failed", status["build"]["error"])
        self.assertIn("finished_at", status["build"])
        self.assertEqual(self.saved[-1]["latest_release"], self.previous_release)
        self.publish.assert_not_called()
        self.lock.assert_not_called()

    def test_failed_status_load_does_not_erase_previous_release(self):
        self.load.side_effect = RuntimeError("GitHub HTTP 503 reading status")
        remote_status = copy.deepcopy(self.previous)
        def save(repository, status, wrapper_sha):
            remote_status.clear()
            remote_status.update(copy.deepcopy(status))
        self.save.side_effect = save
        self.assertEqual(pipeline.run(self.args), 1)
        self.assertEqual(remote_status["latest_release"], self.previous_release,
                         "a read failure must not clobber unknown previous status")
        self.assert_not_built()

    def test_upload_failure_keeps_previous_release_and_pin(self):
        self.args.force = True
        self.publish.side_effect = RuntimeError("upload failed")
        self.assertEqual(pipeline.run(self.args), 1)
        self.assertEqual(self.saved[-1]["latest_release"], self.previous_release)
        self.assertEqual(self.saved[-1]["upstream"]["pinned_sha"], PINNED)
        self.lock.assert_not_called()

    def test_no_change_skips_build_and_retries_outdated_pin(self):
        self.assertEqual(pipeline.run(self.args), 0)
        self.assert_not_built()
        self.lock.assert_called_once_with(REPOSITORY, WRAPPER, TARGET)
        self.assertEqual(self.saved[-1]["latest_release"], self.previous_release)
        self.assertEqual(self.saved[-1]["upstream"]["pinned_sha"], TARGET)
        self.assertEqual(self.saved[-1]["build"]["state"], "success")

    def test_no_change_with_current_pin_records_new_success_not_stale_failure(self):
        # Regression: current implementation returns without updating status.
        self.git.side_effect = lambda *args, **kwargs: TARGET if args == ("rev-parse", "HEAD:upstream") else self.git_response(*args, **kwargs)
        self.assertEqual(pipeline.run(self.args), 0)
        self.assert_not_built()
        self.lock.assert_not_called()
        self.assertTrue(self.saved, "no-change must replace previous failed/building status")
        self.assertEqual(self.saved[-1]["build"]["state"], "success")
        self.assertEqual(self.saved[-1]["build"]["stage"], "complete")
        self.assertIsNone(self.saved[-1]["build"]["error"])

    def test_no_change_lock_repair_has_completion_timestamp(self):
        # Regression: the skip/lock-repair path omits finished_at.
        self.assertEqual(pipeline.run(self.args), 0)
        self.assertTrue(self.saved)
        self.assertIn("finished_at", self.saved[-1]["build"])

    def test_no_change_does_not_claim_pin_update_if_main_has_advanced(self):
        self.lock.return_value = False
        self.assertEqual(pipeline.run(self.args), 0)
        self.assert_not_built()
        self.assertFalse(self.saved[-1]["lock_updated"])
        self.assertEqual(self.saved[-1]["upstream"]["pinned_sha"], PINNED)

    def test_force_cli_and_environment_rebuild_unchanged_inputs(self):
        for force_cli, force_env in ((True, "false"), (False, "TrUe")):
            with self.subTest(cli=force_cli, env=force_env):
                self.args.force = force_cli
                os.environ["FORCE_BUILD"] = force_env
                self.assertEqual(pipeline.run(self.args), 0)
        self.assertEqual(self.prepare.call_count, 2)
        self.assertEqual(self.publish.call_count, 2)

    def test_upstream_or_recipe_change_rebuilds(self):
        for change in ("upstream", "recipe"):
            with self.subTest(change=change):
                previous = copy.deepcopy(self.previous)
                if change == "upstream":
                    previous["latest_release"]["upstream"]["sha"] = PINNED
                else:
                    previous["latest_release"]["wrapper"]["inputs_sha256"] = "e" * 64
                self.load.return_value = previous
                self.assertEqual(pipeline.run(self.args), 0)
        self.assertEqual(self.publish.call_count, 2)

    def test_local_build_never_reads_or_writes_github_or_fetches(self):
        self.args.local = True
        self.args.sha = TARGET
        template = self.write("offline.ini", b"[General]\nEnabled=1\n")
        self.args.template = str(template)
        api = self.patch("gh_api", side_effect=AssertionError("local GitHub access"))
        self.assertEqual(pipeline.run(self.args), 0)
        for operation in (api, self.load, self.save, self.publish, self.lock):
            operation.assert_not_called()
        self.assertFalse(any(call.args[0] in ("fetch", "ls-remote") for call in self.git.call_args_list))
        self.prepare.assert_called_once_with(TARGET)
        config_args = self.stream.call_args_list[1].args[0]
        self.assertEqual(config_args[-2:], ["--template", template.resolve()])
        self.assertIn("-local-", self.package.call_args.args[1])
        status = json.loads((pipeline.DIST / "status.json").read_text())
        self.assertEqual(status["build"]["state"], "success")

    def test_local_failure_also_never_writes_github(self):
        self.args.local = True
        self.stream.side_effect = RuntimeError("offline build failed")
        self.assertEqual(pipeline.run(self.args), 1)
        self.load.assert_not_called()
        self.save.assert_not_called()
        self.publish.assert_not_called()
        self.lock.assert_not_called()

    def test_release_is_published_before_lock_changes(self):
        self.args.force = True
        calls = mock.Mock()
        calls.attach_mock(self.publish, "publish")
        calls.attach_mock(self.lock, "lock")
        self.assertEqual(pipeline.run(self.args), 0)
        self.assertEqual([call[0] for call in calls.mock_calls], ["publish", "lock"])
        self.assertEqual(self.saved[-1]["latest_release"], self.manifest)
        self.assertEqual(self.saved[-1]["build"]["state"], "success")

    def test_lock_failure_after_publish_retains_new_release_for_retry(self):
        self.args.force = True
        self.lock.side_effect = RuntimeError("branch protected")
        self.assertEqual(pipeline.run(self.args), 1)
        self.publish.assert_called_once()
        self.assertEqual(self.saved[-1]["latest_release"], self.manifest)
        self.assertEqual(self.saved[-1]["build"]["stage"], "lock")
        self.assertEqual(self.saved[-1]["upstream"]["pinned_sha"], PINNED)

    def test_remote_target_sha_is_validated_before_prepare(self):
        self.git.side_effect = lambda *args, **kwargs: "--evil\trefs/heads/master" if args[:1] == ("ls-remote",) else self.git_response(*args, **kwargs)
        self.assertEqual(pipeline.run(self.args), 1)
        self.assert_not_built()
        self.assertEqual(self.saved[-1]["latest_release"], self.previous_release)

    def test_cli_disallows_online_sha_and_template_overrides(self):
        for option in ("--sha", "--template"):
            with self.subTest(option=option), mock.patch.object(sys, "argv", ["pipeline.py", "run", option, "value"]):
                with self.assertRaises(SystemExit) as error:
                    pipeline.main()
                self.assertEqual(error.exception.code, 2)
        self.prepare.assert_not_called()


class PublicationTests(PipelineTestCase):
    def test_release_is_draft_then_uploaded_then_published(self):
        events = []
        manifest = {"version": "release-123.2", "upstream": {"url": "https://example/upstream"}, "run_url": "https://example/run"}
        def api(repo, endpoint, **kwargs):
            events.append(("api", endpoint, kwargs))
            return {"id": 42}
        self.patch("gh_api", side_effect=api)
        command = self.patch("command", side_effect=lambda args: events.append(("upload", args)))
        assets = [self.root / "plugin.zip", self.root / "SHA256SUMS"]
        pipeline.publish(REPOSITORY, manifest, assets, WRAPPER)
        self.assertEqual([event[0] for event in events], ["api", "upload", "api"])
        self.assertEqual(events[0][1], "releases")
        self.assertTrue(events[0][2]["body"]["draft"])
        self.assertEqual(events[0][2]["body"]["target_commitish"], WRAPPER)
        command.assert_called_once_with(["gh", "release", "upload", manifest["version"], "--repo", REPOSITORY, *assets])
        self.assertEqual(events[-1][1:], ("releases/42", {"method": "PATCH", "body": {"draft": False, "make_latest": "true"}}))

    def test_upload_failure_never_publishes_draft(self):
        api = self.patch("gh_api", return_value={"id": 42})
        self.patch("command", side_effect=RuntimeError("upload interrupted"))
        with self.assertRaisesRegex(RuntimeError, "upload interrupted"):
            pipeline.publish(REPOSITORY, {"version": "release", "upstream": {"url": "url"}, "run_url": None}, [], WRAPPER)
        self.assertEqual(api.call_count, 1)
        self.assertTrue(api.call_args.kwargs["body"]["draft"])

    def test_update_lock_refuses_already_changed_main_without_writes(self):
        api = self.patch("gh_api", return_value={"object": {"sha": "e" * 40}})
        self.assertFalse(pipeline.update_lock(REPOSITORY, WRAPPER, TARGET))
        api.assert_called_once_with(REPOSITORY, "git/ref/heads/main")

    def test_update_lock_preserves_tree_and_uses_nonforce_update(self):
        api = self.patch("gh_api", side_effect=[
            {"object": {"sha": WRAPPER}}, {"tree": {"sha": "old-tree"}},
            {"sha": "new-tree"}, {"sha": "new-commit"}, {}])
        self.assertTrue(pipeline.update_lock(REPOSITORY, WRAPPER, TARGET))
        api.assert_any_call(REPOSITORY, "git/trees", method="POST", body={
            "base_tree": "old-tree", "tree": [{"path": "upstream", "mode": "160000", "type": "commit", "sha": TARGET}]})
        commit_body = api.call_args_list[3].kwargs["body"]
        self.assertEqual(commit_body["parents"], [WRAPPER])
        api.assert_called_with(REPOSITORY, "git/refs/heads/main", method="PATCH", body={"sha": "new-commit", "force": False})

    def test_update_lock_does_not_retry_with_force_if_main_races(self):
        api = self.patch("gh_api", side_effect=[
            {"object": {"sha": WRAPPER}}, {"tree": {"sha": "old-tree"}},
            {"sha": "new-tree"}, {"sha": "new-commit"}, RuntimeError("HTTP 422: not fast forward")])
        with self.assertRaisesRegex(RuntimeError, "not fast forward"):
            pipeline.update_lock(REPOSITORY, WRAPPER, TARGET)
        self.assertEqual(api.call_count, 5)
        self.assertFalse(api.call_args.kwargs["body"]["force"])

    def test_update_lock_avoids_empty_commit_if_gitlink_already_matches(self):
        api = self.patch("gh_api", side_effect=[
            {"object": {"sha": WRAPPER}}, {"tree": {"sha": "same-tree"}}, {"sha": "same-tree"}])
        self.assertTrue(pipeline.update_lock(REPOSITORY, WRAPPER, TARGET))
        self.assertEqual(api.call_count, 3)


class PackageTests(PipelineTestCase):
    def setUp(self):
        super().setUp()
        self.config = b"[General]\r\nEnabled=1\r\n"
        self.provenance = {"source_url": "https://example/config.ini", "resolved_url": "https://example/config.ini",
                           "repository": None, "sha256": hashlib.sha256(self.config).hexdigest(), "fetched_at": "2026-01-01T00:00:00Z"}
        self.write("dist/config.ini", self.config)
        self.write("dist/config-source.json", json.dumps(self.provenance).encode())
        self.write("dist/build.log", b"build output\n")
        self.write(".work/source/x64/" + pipeline.DLL_NAME, b"MZ fake DLL")
        self.write(".work/source/LICENSE", b"AGPL-3.0")
        self.write(".work/source/build.sh", b"build recipe")
        self.write(".work/source/.build/minhook/src/hook.c", b"minhook source")
        self.write(".work/source/.build/minhook/LICENSE.txt", b"MinHook license")
        self.write(".work/source/.build/minhook/.git/config", b"not corresponding source")
        self.write(".work/source/.build/movsb_shim.c", b"shim")
        self.write("overlay/sync_config.py", b"overlay script")
        self.write(".work/source/sync_config.py", b"overlay script")
        self.patch("command", side_effect=lambda args: "clang fixture\nTarget: x64" if "--version" in args and args[0] != "xwin" else "xwin 0.10.0")
        self.patch("git", side_effect=lambda *args, **kwargs: "LICENSE\0build.sh\0" if args[0] == "ls-files" else "e" * 40)

    def test_package_manifest_checksums_zip_and_full_corresponding_source(self):
        manifest, paths = pipeline.package(REPOSITORY, "release-1", TARGET, WRAPPER, INPUTS, "https://example/run")
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["upstream"]["sha"], TARGET)
        self.assertEqual(manifest["wrapper"]["sha"], WRAPPER)
        self.assertEqual(manifest["wrapper"]["inputs_sha256"], INPUTS)
        self.assertEqual(manifest["config"], self.provenance)
        self.assertEqual(manifest["target"], "windows-x86_64")
        self.assertEqual(manifest["toolchain"]["minhook_sha"], "e" * 40)
        self.assertEqual(json.loads((pipeline.DIST / "update.json").read_text()), manifest)
        assets = {asset["kind"]: asset for asset in manifest["assets"]}
        self.assertEqual(set(assets), {"plugin", "dll", "source", "config", "config-provenance", "build-provenance", "log"})
        for asset in assets.values():
            data = (pipeline.DIST / asset["name"]).read_bytes()
            self.assertEqual(asset["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(asset["size"], len(data))
            self.assertEqual(asset["url"], f"https://github.com/{REPOSITORY}/releases/download/release-1/{asset['name']}")
        with zipfile.ZipFile(pipeline.DIST / assets["plugin"]["name"]) as archive:
            self.assertEqual(set(archive.namelist()), {pipeline.DLL_NAME, "config.ini", "LICENSE", "build-info.json"})
            self.assertEqual(archive.read("config.ini"), self.config)
            self.assertEqual(json.loads(archive.read("build-info.json"))["config"], self.provenance)
        with tarfile.open(pipeline.DIST / assets["source"]["name"]) as archive:
            names = set(archive.getnames())
            self.assertTrue({"source/build.sh", "source/LICENSE", "source/sync_config.py", "source/vendor/minhook/src/hook.c",
                             "source/vendor/minhook/LICENSE.txt", "source/vendor/movsb_shim.c", "source/config.ini",
                             "source/config-source.json", "source/build-info.json", "source/SOURCE-README.txt"} <= names)
            self.assertFalse(any("/.git/" in name for name in names))
        sums = (pipeline.DIST / "SHA256SUMS").read_text().splitlines()
        checked_names = set()
        for line in sums:
            digest, name = line.split("  ", 1)
            self.assertEqual(digest, hashlib.sha256((pipeline.DIST / name).read_bytes()).hexdigest())
            checked_names.add(name)
        self.assertEqual(checked_names, {asset["name"] for asset in assets.values()} | {"update.json"})
        self.assertEqual({path.name for path in paths}, checked_names | {"SHA256SUMS"})

    def test_source_archive_refuses_missing_minhook_source(self):
        (pipeline.SOURCE / ".build/minhook/src/hook.c").unlink()
        with self.assertRaisesRegex(RuntimeError, "MinHook corresponding source missing"):
            pipeline.source_archive(pipeline.DIST / "source.tar.gz")


if __name__ == "__main__":
    unittest.main()
