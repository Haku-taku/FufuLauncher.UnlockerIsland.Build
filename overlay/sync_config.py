#!/usr/bin/env python3
"""Fetch the upstream distribution config verbatim, with provenance.

Only the Python standard library is required. --template selects a local INI
or ZIP instead of the network, for reproducible/offline builds.
"""

import argparse
import configparser
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib import parse, request
import zipfile
import zlib


DEFAULT_SOURCES = (
    "https://raw.githubusercontent.com/CodeCubist/FufuLauncher--Plugins/main/FuFuPlugin.zip",
    "https://raw.githubusercontent.com/FufuLauncher/FufuLauncher--Plugins/main/FuFuPlugin.zip",
)
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
MAX_CONFIG_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 30


class SyncError(Exception):
    """A source could not supply a safe, valid configuration."""


class SafeRedirectHandler(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if parse.urlsplit(newurl).scheme != "https":
            raise SyncError("refusing a redirect to a non-HTTPS URL")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            # urllib otherwise copies Authorization to redirects, including
            # redirects to a different host. No redirect needs our API token.
            redirected.remove_header("Authorization")
        return redirected


def read_limited(stream, limit):
    chunks = []
    size = 0
    while size <= limit:
        chunk = stream.read(min(64 * 1024, limit + 1 - size))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
    raise SyncError(f"content exceeds {limit} byte limit")


def download(url, *, api=False):
    parts = parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise SyncError("sources must be HTTPS URLs without credentials")
    headers = {"User-Agent": "FufuLauncher-config-sync"}
    if api and parts.netloc.lower() == "api.github.com":
        headers["Accept"] = "application/vnd.github+json"
        token = os.environ.get("GH_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    opener = request.build_opener(SafeRedirectHandler())
    try:
        with opener.open(request.Request(url, headers=headers), timeout=TIMEOUT_SECONDS) as response:
            return read_limited(response, MAX_DOWNLOAD_BYTES), response.geturl()
    except HTTPException as exc:
        raise SyncError(f"incomplete or invalid HTTP response: {exc}") from exc


def github_source(url):
    """Recognize GitHub file URLs; refs with slashes must be URL-encoded."""
    parts = parse.urlsplit(url)
    if parts.scheme != "https" or parts.query or parts.fragment:
        return None
    segments = parts.path.strip("/").split("/")
    host = parts.netloc.lower()
    if host == "github.com" and len(segments) >= 5 and segments[2] in ("blob", "raw"):
        segments.pop(2)
    elif host != "raw.githubusercontent.com":
        return None
    if len(segments) < 4 or not all(segments):
        return None
    owner, repo, ref = (parse.unquote(value) for value in segments[:3])
    return f"{owner}/{repo}", ref, "/".join(segments[3:])


def resolve_source(source):
    metadata = {"source_url": source, "resolved_url": source, "repository": None}
    github = github_source(source)
    if github is None:
        return metadata
    repository, ref, file_path = github
    metadata["repository"] = repository
    revision = ref if re.fullmatch(r"[0-9a-fA-F]{40}", ref) else None
    if revision is None:
        api_url = ("https://api.github.com/repos/"
                   f"{parse.quote(repository, safe='/')}/commits/{parse.quote(ref, safe='')}")
        try:
            payload, _ = download(api_url, api=True)
            commit = json.loads(payload)
            candidate = commit.get("sha") if isinstance(commit, dict) else None
            if isinstance(candidate, str) and re.fullmatch(r"[0-9a-fA-F]{40}", candidate):
                revision = candidate
        except (SyncError, OSError, ValueError):
            # Public API rate limits or outages must not prevent fetching the
            # distribution. A mutable fallback never claims a resolved revision.
            pass
    if revision:
        metadata["revision"] = revision
        metadata["resolved_url"] = (
            f"https://raw.githubusercontent.com/{parse.quote(repository, safe='/')}/"
            f"{revision}/{file_path}"
        )
    return metadata


def config_bytes(payload):
    stream = io.BytesIO(payload)
    if zipfile.is_zipfile(stream):
        try:
            with zipfile.ZipFile(stream) as archive:
                candidates = []
                roots = []
                for entry in archive.infolist():
                    name = entry.filename.replace("\\", "/")
                    if not entry.is_dir() and name.rsplit("/", 1)[-1].casefold() == "config.ini":
                        candidates.append(entry)
                        if "/" not in name:
                            roots.append(entry)
                selected = roots if roots else candidates
                if len(selected) != 1:
                    raise SyncError("ZIP must contain a unique root config.ini or unique nested config.ini")
                entry = selected[0]
                if entry.file_size > MAX_CONFIG_BYTES:
                    raise SyncError("extracted config.ini exceeds size limit")
                with archive.open(entry) as member:
                    payload = read_limited(member, MAX_CONFIG_BYTES)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error) as exc:
            raise SyncError(f"cannot read ZIP: {exc}") from exc
    if len(payload) > MAX_CONFIG_BYTES:
        raise SyncError("config.ini exceeds size limit")
    for encoding in ("utf-8-sig", "gbk"):
        try:
            text = payload.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise SyncError("config.ini must be UTF-8 or GBK")
    if "\x00" in text:
        raise SyncError("config.ini contains NUL bytes")
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        raise SyncError(f"invalid config.ini: {exc}") from exc
    if not parser.sections():
        raise SyncError("config.ini must contain configuration sections")
    return payload


def fetch_config(sources, template=None):
    if template is not None:
        template = Path(template).resolve()
        with template.open("rb") as stream:
            payload = read_limited(stream, MAX_DOWNLOAD_BYTES)
        metadata = {"source_url": template.as_uri(), "resolved_url": template.as_uri(),
                    "repository": None}
        return config_bytes(payload), metadata
    failures = []
    for source in sources:
        try:
            metadata = resolve_source(source)
            payload, final_url = download(metadata["resolved_url"])
            data = config_bytes(payload)
            metadata["resolved_url"] = final_url
            return data, metadata
        except (SyncError, OSError, ValueError) as exc:
            failures.append(f"{source}: {exc}")
    raise SyncError("all configuration sources failed:\n" + "\n".join(failures))


def stage_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def sync(output, metadata_path, sources=DEFAULT_SOURCES, template=None):
    output, metadata_path = Path(output), Path(metadata_path)
    if output.resolve() == metadata_path.resolve():
        raise SyncError("output and metadata paths must differ")
    data, metadata = fetch_config(sources, template)
    metadata["sha256"] = hashlib.sha256(data).hexdigest()
    metadata["fetched_at"] = datetime.now(timezone.utc).isoformat()
    metadata_bytes = (json.dumps(metadata, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    staged = []
    try:
        staged.append(stage_file(output, data))
        staged.append(stage_file(metadata_path, metadata_bytes))
        # Stage both files first. Publish the config last so a metadata staging
        # or publication failure leaves the previous config untouched.
        os.replace(staged[1], metadata_path)
        os.replace(staged[0], output)
    finally:
        for temporary in staged:
            temporary.unlink(missing_ok=True)
    return metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--source", action="append", help="HTTPS source URL; repeat for fallbacks")
    parser.add_argument("--template", type=Path, help="local plain INI or ZIP (no network)")
    args = parser.parse_args(argv)
    try:
        sync(args.output, args.metadata, args.source or DEFAULT_SOURCES, args.template)
    except (SyncError, OSError, ValueError) as exc:
        print(f"sync_config: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
