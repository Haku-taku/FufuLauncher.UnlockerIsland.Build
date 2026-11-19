#!/usr/bin/env python3
"""Apply a local patch queue to a locked upstream and publish xwin builds."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "upstream"
SOURCE = ROOT / ".work/source"
DIST = ROOT / "dist"
UPSTREAM_REPO = "FufuLauncher/FufuLauncher.UnlockerIsland"
UPSTREAM_BRANCH = "master"
DEFAULT_REPO = "Haku-taku/FufuLauncher.UnlockerIsland.Build"
DLL_NAME = "FufuLauncher.UnlockerIsland.dll"
SHA = re.compile(r"[0-9a-f]{40}")


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def command(args, cwd=ROOT, *, text=True):
    result = subprocess.run([str(a) for a in args], cwd=cwd, capture_output=True,
                            text=text, timeout=180)
    if result.returncode:
        error = result.stderr if text else result.stderr.decode(errors="replace")
        output = result.stdout if text else result.stdout.decode(errors="replace")
        raise RuntimeError(f"{' '.join(str(a) for a in args)}\n{error}\n{output}")
    return result.stdout.strip() if text else result.stdout


def git(*args, cwd=ROOT):
    return command(["git", "--no-pager", *args], cwd=cwd)


def valid_sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError(f"expected a full lowercase commit SHA, got {value!r}")
    return value


def fingerprint():
    """Ignore the gitlink and docs: bot lock commits must not trigger rebuilds."""
    digest = hashlib.sha256()
    for directory in ("patches", "overlay", "scripts", ".github/workflows"):
        for path in sorted((ROOT / directory).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0")
                digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def gh_api(repository, endpoint, *, method="GET", body=None, missing_ok=False):
    args = ["gh", "api", "--method", method, f"repos/{repository}/{endpoint}",
            "-H", "Accept: application/vnd.github+json",
            "-H", "X-GitHub-Api-Version: 2022-11-28"]
    data = None
    if body is not None:
        args += ["--input", "-"]
        data = json.dumps(body)
    result = subprocess.run(args, input=data, capture_output=True, text=True,
                            cwd=ROOT, timeout=120)
    if result.returncode:
        if missing_ok and "HTTP 404" in result.stderr:
            return None
        raise RuntimeError(f"GitHub {method} {endpoint}: {result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def load_status(repository):
    response = gh_api(repository, "contents/status.json?ref=status", missing_ok=True)
    if response is None:
        return {}
    return json.loads(base64.b64decode(response["content"]))


def save_status(repository, status, wrapper_sha):
    status["updated_at"] = now()
    DIST.mkdir(exist_ok=True)
    raw = (json.dumps(status, ensure_ascii=False, indent=2) + "\n").encode()
    (DIST / "status.json").write_bytes(raw)
    if gh_api(repository, "git/ref/heads/status", missing_ok=True) is None:
        gh_api(repository, "git/refs", method="POST",
               body={"ref": "refs/heads/status", "sha": wrapper_sha})
    old = gh_api(repository, "contents/status.json?ref=status", missing_ok=True)
    body = {"message": f"ci: {status['build']['state']} ({status['build']['stage']})",
            "content": base64.b64encode(raw).decode(), "branch": "status"}
    if old is not None:
        body["sha"] = old["sha"]
    gh_api(repository, "contents/status.json", method="PUT", body=body)


def prepare(sha):
    """Only recreate our registered disposable worktree, never a user's tree."""
    valid_sha(sha)
    git("cat-file", "-e", f"{sha}^{{commit}}", cwd=UPSTREAM)
    if SOURCE.exists():
        registered = git("worktree", "list", "--porcelain", cwd=UPSTREAM)
        if f"worktree {SOURCE}\n" not in registered + "\n":
            raise RuntimeError(f"refusing to remove unregistered directory: {SOURCE}")
        git("worktree", "remove", "--force", str(SOURCE), cwd=UPSTREAM)
    SOURCE.parent.mkdir(exist_ok=True)
    git("worktree", "add", "--detach", str(SOURCE), sha, cwd=UPSTREAM)
    git("config", "user.name", "UnlockerIsland build", cwd=SOURCE)
    git("config", "user.email", "build@localhost", cwd=SOURCE)
    patches = sorted((ROOT / "patches").glob("*.patch"))
    if not patches:
        raise RuntimeError("patch queue is empty")
    for patch in patches:
        print(f"Applying {patch.name}", flush=True)
        git("apply", "--3way", "--index", str(patch), cwd=SOURCE)
    for path in (ROOT / "overlay").rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        target = SOURCE / path.relative_to(ROOT / "overlay")
        if not target.resolve().is_relative_to(SOURCE.resolve()):
            raise RuntimeError(f"overlay destination escapes worktree: {target}")
        if target.is_symlink():
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    return SOURCE


def stream(args, cwd):
    DIST.mkdir(exist_ok=True)
    with (DIST / "build.log").open("a", encoding="utf-8") as log:
        log.write(f"\n>>> {' '.join(str(a) for a in args)}\n")
        log.flush()
        process = subprocess.Popen([str(a) for a in args], cwd=cwd,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors="replace", bufsize=1)
        for line in process.stdout:
            print(line, end="", flush=True)
            log.write(line)
            log.flush()
        if process.wait():
            raise RuntimeError(f"command failed ({process.returncode}): {args[0]}; see build.log")


def file_asset(path, repository, version, kind):
    return {"name": path.name, "kind": kind, "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "url": f"https://github.com/{repository}/releases/download/{version}/{path.name}"}


def source_archive(destination):
    """Ship full patched corresponding source, not just a GitHub gitlink archive."""
    paths = set(git("ls-files", "-z", cwd=SOURCE).split("\0")) - {""}
    paths.update(str(p.relative_to(ROOT / "overlay")) for p in (ROOT / "overlay").rglob("*")
                 if p.is_file() and "__pycache__" not in p.parts)
    minhook = SOURCE / ".build/minhook"
    if not (minhook / "src/hook.c").is_file():
        raise RuntimeError("MinHook corresponding source missing from .build/minhook")
    with tarfile.open(destination, "w:gz") as archive:
        for relative in sorted(paths):
            path = SOURCE / relative
            if path.is_file() or path.is_symlink():
                archive.add(path, arcname=f"source/{relative}", recursive=False)
        for path in sorted(minhook.rglob("*")):
            if path.is_file() and ".git" not in path.relative_to(minhook).parts:
                archive.add(path, arcname=f"source/vendor/minhook/{path.relative_to(minhook)}")
        archive.add(SOURCE / ".build/movsb_shim.c", arcname="source/vendor/movsb_shim.c")
        for name in ("build-info.json", "config-source.json", "config.ini"):
            archive.add(DIST / name, arcname=f"source/{name}")
        instructions = DIST / "SOURCE-README.txt"
        instructions.write_text(
            "Full patched source for this release, including statically linked MinHook.\n"
            "License: AGPL-3.0; see LICENSE and vendor/minhook/LICENSE.txt.\n"
            "On Linux install LLVM (clang-cl, lld-link, llvm-lib, llvm-ml, llvm-rc) and xwin 0.10.0.\n"
            "Build from source/: MINHOOK_SRC=vendor/minhook bash build.sh\n"
            "xwin downloads Microsoft's SDK/CRT under Microsoft's license terms.\n"
            "build-info.json records the upstream, build-recipe and MinHook revisions.\n",
            encoding="utf-8")
        archive.add(instructions, arcname="source/SOURCE-README.txt")


def package(repository, version, upstream_sha, wrapper_sha, inputs, run_url):
    config = json.loads((DIST / "config-source.json").read_text())
    info = {"schema_version": 1, "version": version, "built_at": now(),
            "upstream": {"repository": UPSTREAM_REPO, "branch": UPSTREAM_BRANCH,
                         "sha": upstream_sha,
                         "url": f"https://github.com/{UPSTREAM_REPO}/commit/{upstream_sha}"},
            "wrapper": {"repository": repository, "sha": wrapper_sha,
                        "inputs_sha256": inputs},
            "config": config, "run_url": run_url, "target": "windows-x86_64",
            "toolchain": {"clang": command([os.getenv("CLANG_CL", "clang-cl"), "--version"]).splitlines()[0],
                          "xwin": command(["xwin", "--version"]),
                          "minhook_sha": git("rev-parse", "HEAD", cwd=SOURCE / ".build/minhook")}}
    (DIST / "build-info.json").write_text(json.dumps(info, indent=2) + "\n")
    shutil.copy2(SOURCE / "x64" / DLL_NAME, DIST / DLL_NAME)
    shutil.copy2(SOURCE / "LICENSE", DIST / "LICENSE")
    plugin_zip = DIST / "FuFuPlugin-windows-x86_64.zip"
    with zipfile.ZipFile(plugin_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in (DLL_NAME, "config.ini", "LICENSE", "build-info.json"):
            archive.write(DIST / name, arcname=name)
    source_tar = DIST / "FuFuPlugin-source.tar.gz"
    source_archive(source_tar)
    assets = [file_asset(path, repository, version, kind) for path, kind in (
        (plugin_zip, "plugin"), (DIST / DLL_NAME, "dll"), (source_tar, "source"),
        (DIST / "config.ini", "config"), (DIST / "config-source.json", "config-provenance"),
        (DIST / "build-info.json", "build-provenance"), (DIST / "build.log", "log"))]
    manifest = {**info, "release_url": f"https://github.com/{repository}/releases/tag/{version}",
                "assets": assets}
    (DIST / "update.json").write_text(json.dumps(manifest, indent=2) + "\n")
    checksum_paths = [DIST / a["name"] for a in assets] + [DIST / "update.json"]
    (DIST / "SHA256SUMS").write_text("".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in checksum_paths))
    return manifest, checksum_paths + [DIST / "SHA256SUMS"]


def publish(repository, manifest, assets, wrapper_sha):
    version = manifest["version"]
    body = (f"Automated xwin / LLVM Windows x64 build.\n\n"
            f"Upstream: {manifest['upstream']['url']}\n\n"
            f"Build recipe: https://github.com/{repository}/commit/{wrapper_sha}\n\n"
            f"Workflow: {manifest['run_url']}\n\n"
            "Download FuFuPlugin-windows-x86_64.zip. Read update.json for provenance and SHA-256.\n"
            "FuFuPlugin-source.tar.gz is the full patched corresponding source (including MinHook).\n"
            "config.ini is the current distribution template; replacing a user config resets its settings.\n")
    draft = gh_api(repository, "releases", method="POST", body={
        "tag_name": version, "target_commitish": wrapper_sha, "name": version,
        "body": body, "draft": True, "prerelease": False})
    command(["gh", "release", "upload", version, "--repo", repository, *assets])
    gh_api(repository, f"releases/{draft['id']}", method="PATCH",
           body={"draft": False, "make_latest": "true"})


def update_lock(repository, wrapper_sha, sha):
    """Compare-and-swap main. A newer recipe must never be overwritten."""
    branch = gh_api(repository, "git/ref/heads/main")
    if branch["object"]["sha"] != wrapper_sha:
        print("main advanced during build; leaving its submodule pin untouched", flush=True)
        return False
    commit = gh_api(repository, f"git/commits/{wrapper_sha}")
    tree = gh_api(repository, "git/trees", method="POST", body={
        "base_tree": commit["tree"]["sha"], "tree": [
            {"path": "upstream", "mode": "160000", "type": "commit", "sha": sha}]})
    if tree["sha"] == commit["tree"]["sha"]:
        return True
    new_commit = gh_api(repository, "git/commits", method="POST", body={
        "message": f"ci: lock successful upstream {sha[:12]}",
        "tree": tree["sha"], "parents": [wrapper_sha]})
    gh_api(repository, "git/refs/heads/main", method="PATCH",
           body={"sha": new_commit["sha"], "force": False})
    return True


def initial_status(wrapper_sha, previous):
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    repository = os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO)
    pinned = valid_sha(git("rev-parse", "HEAD:upstream"))
    return {"schema_version": 1, "updated_at": now(),
            "upstream": {"repository": UPSTREAM_REPO, "branch": UPSTREAM_BRANCH,
                         "pinned_sha": pinned, "latest_sha": None},
            "build": {"state": "building", "stage": "detect", "error": None,
                      "attempt_id": f"{run_id}.{attempt}", "wrapper_sha": wrapper_sha,
                      "run_url": f"https://github.com/{repository}/actions/runs/{run_id}" if run_id != "local" else None},
            "latest_release": previous.get("latest_release")}


def run(args):
    repository = os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO)
    wrapper_sha = valid_sha(git("rev-parse", "HEAD"))
    status = initial_status(wrapper_sha, {})
    DIST.mkdir(exist_ok=True)
    (DIST / "build.log").write_text("")
    previous = {}
    previous_loaded = args.local
    try:
        if not args.local:
            previous = load_status(repository)
            previous_loaded = True
            status["latest_release"] = previous.get("latest_release")
        pinned = status["upstream"]["pinned_sha"]
        if args.local:
            target = valid_sha(args.sha or pinned)
        else:
            target = valid_sha(git("ls-remote", "origin", f"refs/heads/{UPSTREAM_BRANCH}",
                                   cwd=UPSTREAM).split()[0])
        status["upstream"]["latest_sha"] = target
        inputs = fingerprint()
        last = status.get("latest_release") or {}
        unchanged = last.get("upstream", {}).get("sha") == target and last.get("wrapper", {}).get("inputs_sha256") == inputs
        force = args.force or os.environ.get("FORCE_BUILD", "false").lower() == "true"
        if not args.local and unchanged and not force:
            if pinned != target:
                status["lock_updated"] = update_lock(repository, wrapper_sha, target)
                if status["lock_updated"]:
                    status["upstream"]["pinned_sha"] = target
            status["build"].update(state="success", stage="complete", finished_at=now())
            if pinned != target or previous.get("build", {}).get("state") != "success":
                save_status(repository, status, wrapper_sha)
            print("Upstream and build inputs unchanged; no new release needed.")
            return 0
        def report(stage):
            status["build"]["stage"] = stage
            if not args.local:
                save_status(repository, status, wrapper_sha)
        report("fetch")
        if not args.local:
            git("fetch", "origin", UPSTREAM_BRANCH, cwd=UPSTREAM)
        report("patch")
        prepare(target)
        report("compile")
        stream(["bash", "build.sh"], SOURCE)
        report("config")
        config_args = [sys.executable, "sync_config.py", "--output", DIST / "config.ini",
                       "--metadata", DIST / "config-source.json"]
        if args.template:
            config_args += ["--template", Path(args.template).resolve()]
        stream(config_args, SOURCE)
        report("package")
        suffix = status["build"]["attempt_id"]
        if args.local:
            suffix = datetime.now(timezone.utc).strftime("local-%Y%m%dT%H%M%SZ")
        version = f"upstream-{target[:12]}-{suffix}"
        manifest, assets = package(repository, version, target, wrapper_sha, inputs,
                                   status["build"]["run_url"])
        if not args.local:
            report("release")
            publish(repository, manifest, assets, wrapper_sha)
            status["latest_release"] = manifest
            report("lock")
            status["lock_updated"] = update_lock(repository, wrapper_sha, target)
            if status["lock_updated"]:
                status["upstream"]["pinned_sha"] = target
        else:
            status["latest_release"] = manifest
        status["build"].update(state="success", stage="complete", finished_at=now())
        if not args.local:
            save_status(repository, status, wrapper_sha)
        else:
            (DIST / "status.json").write_text(json.dumps(status, indent=2) + "\n")
        print(f"Completed {version}", flush=True)
        return 0
    except Exception as error:
        status["build"].update(state="failed", error=str(error)[-12000:], finished_at=now())
        with (DIST / "build.log").open("a") as log:
            log.write(f"\nFAILED at {status['build']['stage']}: {error}\n")
        (DIST / "status.json").write_text(json.dumps(status, indent=2) + "\n")
        if not args.local and previous_loaded:
            try:
                save_status(repository, status, wrapper_sha)
            except Exception as report_error:
                print(f"Status publishing also failed: {report_error}", file=sys.stderr)
        print(f"FAILED at {status['build']['stage']}: {error}", file=sys.stderr)
        return 1


def report_setup_failure():
    repository = os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO)
    wrapper_sha = valid_sha(git("rev-parse", "HEAD"))
    status = initial_status(wrapper_sha, load_status(repository))
    try:
        status["upstream"]["latest_sha"] = valid_sha(git(
            "ls-remote", "origin", f"refs/heads/{UPSTREAM_BRANCH}", cwd=UPSTREAM).split()[0])
    except Exception as error:
        print(f"Could not detect latest upstream: {error}", file=sys.stderr)
    status["build"].update(state="failed", stage="setup", error="Workflow setup failed; see run_url", finished_at=now())
    save_status(repository, status, wrapper_sha)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    prepare_parser = commands.add_parser("prepare", help="recreate disposable patched source without publishing")
    prepare_parser.add_argument("--sha")
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--local", action="store_true", help="build/package pinned source without GitHub writes")
    run_parser.add_argument("--sha", help="local-only upstream SHA")
    run_parser.add_argument("--template", help="local-only offline config template")
    run_parser.add_argument("--force", action="store_true")
    commands.add_parser("report-setup-failure")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.sha or valid_sha(git("rev-parse", "HEAD:upstream")))
        print(SOURCE)
    elif args.action == "report-setup-failure":
        report_setup_failure()
    else:
        if not args.local and (args.sha or args.template):
            parser.error("--sha and --template are only allowed with --local")
        return run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
