#!/usr/bin/env python3
"""Archive public GitHub repository conversations and attachments (stdlib only)."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
URL_RE = re.compile(r'https://[^\s<>"\x27]+')
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
CHUNK_BYTES = 48 * 1024 * 1024


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def dump_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verified_asset(root, entry):
    """Verify small files or ordered chunks against the original whole-file digest."""
    digest, size = hashlib.sha256(), 0
    pieces = entry.get("parts") or [{"path": entry.get("path", "")}]
    for piece in pieces:
        path = (root / piece["path"]).resolve()
        if not path.is_relative_to(root / "assets") or not path.is_file():
            return False
        part_digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                part_digest.update(chunk)
                size += len(chunk)
        if piece.get("sha256") and piece["sha256"] != part_digest.hexdigest():
            return False
    return digest.hexdigest() == entry.get("sha256") and size == entry.get("bytes")


def chunk_asset(target, root, chunk_bytes=CHUNK_BYTES):
    """Losslessly split oversized Git files; retain an independently checkable manifest."""
    if target.stat().st_size <= chunk_bytes:
        return {}
    directory = target.with_name(target.name + ".parts")
    directory.mkdir(exist_ok=True)
    if not directory.resolve().is_relative_to(root / "assets"):
        raise RuntimeError("Chunk directory points outside the archive")
    parts = []
    with target.open("rb") as source:
        index = 0
        while data := source.read(chunk_bytes):
            index += 1
            piece = directory / f"{index:05d}.part"
            piece.write_bytes(data)
            parts.append({"path": piece.relative_to(root).as_posix(), "bytes": len(data),
                          "sha256": hashlib.sha256(data).hexdigest()})
    # An interrupted or changed prior run must not leave stale tail chunks.
    for old in directory.glob("*.part"):
        if old.name not in {Path(piece["path"]).name for piece in parts}:
            old.unlink()
    readme = directory / "README.md"
    readme.write_text("# Chunked attachment\n\nOriginal filename: `" + target.name + "`\n\n"
                      "This file exceeds the archive chunk size. All bytes are preserved in ordered parts. "
                      "Use `python tools/restore_archive.py PATH_TO_ARCHIVE OUTPUT_DIRECTORY` to reconstruct and verify it. "
                      "The archive manifest records every part and the original SHA-256.\n", encoding="utf-8")
    target.unlink()
    return {"parts": parts, "path": readme.relative_to(root).as_posix(), "filename": target.name}


def safe_download_url(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        return (parsed.scheme == "https" and not parsed.username and not parsed.password
                and parsed.port in (None, 443)
                and (host in {"github.com", "api.github.com", "githubusercontent.com",
                              "github-production-user-asset-6210df.s3.amazonaws.com"}
                     or host.endswith(".githubusercontent.com")))
    except ValueError:
        return False


def is_attachment(url):
    if not safe_download_url(url):
        return False
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname
    if host in {"user-images.githubusercontent.com", "private-user-images.githubusercontent.com"}:
        return True
    if host == "cloud.githubusercontent.com" and parsed.path.startswith("/assets/"):
        return True
    return host == "github.com" and bool(re.match(
        r"/(?:user-attachments/(?:assets|files)/|[^/]+/[^/]+/(?:files|assets)/)", parsed.path))


def attachment_urls(value):
    """Find uploaded media in Markdown, HTML, and bare links, including nested comments."""
    found = set()
    if isinstance(value, dict):
        for child in value.values():
            found.update(attachment_urls(child))
    elif isinstance(value, list):
        for child in value:
            found.update(attachment_urls(child))
    elif isinstance(value, str):
        for match in URL_RE.finditer(html.unescape(value)):
            candidate = match.group().rstrip(".,;:")
            while candidate.endswith(")") and candidate.count(")") > candidate.count("("):
                candidate = candidate[:-1]
            candidate = candidate.split("#", 1)[0]
            if is_attachment(candidate):
                found.add(candidate)
    return found


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not safe_download_url(newurl):
            raise RuntimeError("Download redirected outside GitHub's permitted HTTPS hosts")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        # API credentials must never follow a signed redirect to an asset CDN.
        if urllib.parse.urlsplit(newurl).hostname != "api.github.com":
            redirected.remove_header("Authorization")
        return redirected


class Client:
    def __init__(self, token="", opener=None):
        self.token = token
        self.opener = opener or urllib.request.build_opener(SafeRedirect())

    def open(self, url, binary=False):
        if not safe_download_url(url):
            raise RuntimeError("Refusing a non-GitHub HTTPS URL")
        headers = {"User-Agent": "repository-dump/1.0", "Accept": "application/octet-stream" if binary else "application/vnd.github+json"}
        if urllib.parse.urlsplit(url).hostname == "api.github.com":
            headers["X-GitHub-Api-Version"] = "2022-11-28"
            if self.token:
                headers["Authorization"] = "Bearer " + self.token
        for attempt in range(3):
            try:
                return self.opener.open(urllib.request.Request(url, headers=headers), timeout=90)
            except urllib.error.HTTPError as error:
                if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise RuntimeError(f"GitHub HTTP {error.code}") from None
                delay = error.headers.get("Retry-After", str(2 ** attempt))
                time.sleep(min(int(delay) if delay.isdigit() else 2, 30))
        raise RuntimeError("GitHub request failed")

    def json(self, path):
        with self.open(API + path) as response:
            return json.load(response)

    def pages(self, path):
        separator = "&" if "?" in path else "?"
        url = API + path + separator + "per_page=100"
        result, seen = [], set()
        while url:
            if url in seen or not url.startswith(API + "/"):
                raise RuntimeError("Invalid/repeated API pagination link")
            seen.add(url)
            with self.open(url) as response:
                items = json.load(response)
                if not isinstance(items, list):
                    raise RuntimeError("Expected a paginated JSON list")
                result.extend(items)
                links = response.headers.get("Link", "")
            match = re.search(r'<([^>]+)>;\s*rel="next"', links)
            url = match.group(1) if match else None
        return result


class Archive:
    def __init__(self, client, repository, output, max_asset_bytes, max_total_bytes):
        self.client, self.repository = client, repository
        self.output = Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.max_asset_bytes, self.max_total_bytes = max_asset_bytes, max_total_bytes
        self.total_bytes = 0
        self.assets, self.errors, self.previous = {}, [], {}
        self.record_paths = {"issues": [], "pulls": [], "releases": []}
        old_manifest = self.output / "manifest.json"
        if old_manifest.exists():
            previous_manifest = json.loads(old_manifest.read_text(encoding="utf-8"))
            if previous_manifest.get("repository") != repository:
                raise ValueError("Output directory belongs to a different repository")
            self.previous = previous_manifest.get("assets", {})
        self.manifest = {"schema_version": 1, "repository": repository, "started_at": now(),
                         "complete": False, "counts": {}, "assets": self.assets, "errors": self.errors}

    def download(self, url, source, api_url=None):
        if url in self.assets:
            if source not in self.assets[url]["sources"]:
                self.assets[url]["sources"].append(source)
            return
        entry = {"sources": [source], "status": "failed"}
        self.assets[url] = entry
        partial = None
        try:
            prior = self.previous.get(url, {})
            if prior.get("status") == "saved" and verified_asset(self.output, prior):
                size = prior["bytes"]
                if size > self.max_asset_bytes or self.total_bytes + size > self.max_total_bytes:
                    raise RuntimeError("Verified cached asset exceeds configured size budget")
                entry.update({key: prior[key] for key in ("path", "sha256", "bytes", "content_type")})
                if prior.get("parts"):
                    entry.update(parts=prior["parts"], filename=prior["filename"])
                elif size > CHUNK_BYTES:
                    entry.update(chunk_asset(self.output / prior["path"], self.output))
                entry["status"] = "saved"
                entry["reused"] = True
                self.total_bytes += size
                return
            with self.client.open(api_url or url, binary=True) as response:
                content_type = response.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0]
                disposition = response.headers.get("Content-Disposition", "")
                requested_path = urllib.parse.urlsplit(url).path.lower()
                if (content_type == "text/html" and "attachment" not in disposition.lower()
                        and not requested_path.endswith((".html", ".htm"))):
                    raise RuntimeError("Received an HTML page instead of attachment bytes")
                if api_url and content_type == "application/json" and "attachment" not in disposition.lower():
                    raise RuntimeError("Release asset API returned metadata instead of file bytes")
                declared = int(response.headers.get("Content-Length", "0"))
                if declared > self.max_asset_bytes or self.total_bytes + declared > self.max_total_bytes:
                    raise RuntimeError("Attachment exceeds configured size budget")
                name = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).name
                name = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:110] or "attachment"
                if not Path(name).suffix:
                    name += mimetypes.guess_extension(content_type) or ".bin"
                filename = hashlib.sha256(url.encode()).hexdigest()[:20] + "-" + name
                target = self.output / "assets" / filename
                target.parent.mkdir(exist_ok=True)
                if not target.parent.resolve().is_relative_to(self.output):
                    raise RuntimeError("Asset directory points outside the output directory")
                partial = target.with_suffix(target.suffix + ".part")
                if not target.resolve().is_relative_to(self.output) or not partial.resolve().is_relative_to(self.output):
                    raise RuntimeError("Asset file points outside the output directory")
                digest, size = hashlib.sha256(), 0
                with partial.open("wb") as stream:
                    for chunk in iter(lambda: response.read(1024 * 1024), b""):
                        size += len(chunk)
                        if size > self.max_asset_bytes or self.total_bytes + size > self.max_total_bytes:
                            raise RuntimeError("Attachment exceeds configured size budget")
                        stream.write(chunk)
                        digest.update(chunk)
                if declared and size != declared:
                    raise RuntimeError("Truncated attachment download")
                partial.replace(target)
                entry.update(status="saved", path=target.relative_to(self.output).as_posix(),
                             sha256=digest.hexdigest(), bytes=size, content_type=content_type, reused=False)
                entry.update(chunk_asset(target, self.output))
                self.total_bytes += size
        except Exception as error:
            entry["error"] = str(error).split("https://", 1)[0][:200]
            self.errors.append({"source": source, "asset": url, "error": entry["error"]})
        finally:
            if partial and partial.exists():
                partial.unlink()

    def save_record(self, folder, identifier, record):
        destination = self.output / folder / str(identifier)
        self.record_paths[folder].append(str(identifier))
        dump_json(destination.with_suffix(".json"), record)
        for url in sorted(attachment_urls(record)):
            self.download(url, f"{folder}/{identifier}")
        lines = [f"# {record.get('title') or record.get('name') or record.get('tag_name') or identifier}",
                 "", record.get("html_url", ""), "", record.get("body") or ""]
        for field in ("comments", "reviews", "review_comments"):
            for item in record.get(field, []):
                lines += ["", f"## {field}: {(item.get('user') or {}).get('login', 'deleted user')} · {item.get('created_at') or item.get('submitted_at') or ''}",
                          "", item.get("html_url", ""), "", item.get("body") or ""]
        if folder == "releases":
            lines += ["", "## Release files"]
            lines += [f"- [{item.get('name', 'asset')}]({item['browser_download_url']})" for item in record.get("assets", [])]
        rendered = "\n".join(lines) + "\n"
        for url, asset in sorted(self.assets.items(), key=lambda pair: -len(pair[0])):
            if asset["status"] == "saved":
                rendered = rendered.replace(url, "../" + asset["path"])
        destination.with_suffix(".md").write_text(rendered, encoding="utf-8")

    def run(self):
        prefix = "/repos/" + self.repository
        try:
            metadata = self.client.json(prefix)
            if metadata.get("private"):
                raise RuntimeError("This exporter intentionally supports public repositories only")
            dump_json(self.output / "metadata" / "repository.json", metadata)
            issues = [i for i in self.client.pages(prefix + "/issues?state=all&sort=created&direction=asc") if "pull_request" not in i]
            pulls = self.client.pages(prefix + "/pulls?state=all&sort=created&direction=asc")
            releases = self.client.pages(prefix + "/releases")
            tags = self.client.pages(prefix + "/tags")
            dump_json(self.output / "metadata" / "tags.json", tags)
            self.manifest["counts"] = {"issues": len(issues), "pull_requests": len(pulls), "releases": len(releases), "tags": len(tags)}
            for item in issues:
                number = item["number"]
                item["comments"] = self.client.pages(f"{prefix}/issues/{number}/comments")
                self.save_record("issues", number, item)
                print(f"Saved issue #{number}", flush=True)
            for item in pulls:
                number = item["number"]
                item = self.client.json(f"{prefix}/pulls/{number}")
                item["comments"] = self.client.pages(f"{prefix}/issues/{number}/comments")
                item["reviews"] = self.client.pages(f"{prefix}/pulls/{number}/reviews")
                item["review_comments"] = self.client.pages(f"{prefix}/pulls/{number}/comments")
                item["changed_files"] = self.client.pages(f"{prefix}/pulls/{number}/files")
                self.save_record("pulls", number, item)
                print(f"Saved pull request #{number}", flush=True)
            for item in releases:
                item["assets"] = self.client.pages(f"{prefix}/releases/{item['id']}/assets")
                for asset in item["assets"]:
                    self.download(asset["browser_download_url"], f"releases/{item['id']}", asset["url"])
                self.save_record("releases", item["id"], item)
                print(f"Saved release {item.get('tag_name', item['id'])}", flush=True)
            self.manifest["complete"] = not self.errors
        except Exception as error:
            self.errors.append({"source": "api", "error": str(error).split("https://", 1)[0][:200]})
        finally:
            self.manifest["finished_at"] = now()
            self.manifest["asset_bytes"] = self.total_bytes
            self.manifest["asset_count"] = len(self.assets)
            self.manifest["records"] = self.record_paths
            dump_json(self.output / "manifest.json", self.manifest)
            self.write_index()
        return 0 if self.manifest["complete"] else 2

    def write_index(self):
        lines = [f"# Repository archive: {self.repository}", "",
                 f"Status: **{'Complete for the documented API scope' if self.manifest['complete'] else 'INCOMPLETE — inspect manifest errors'}**", "",
                 f"Started: {self.manifest['started_at']}  ", f"Finished: {self.manifest['finished_at']}", "",
                 "This is a sequential API snapshot, not an atomic point-in-time backup. Deleted/private data and GitHub Discussions are outside the scope.", "",
                 "[Manifest, checksums and errors](manifest.json) · [Repository metadata](metadata/repository.json) · [Tags](metadata/tags.json)", ""]
        for folder, heading in (("issues", "Issues"), ("pulls", "Pull requests"), ("releases", "Releases")):
            lines += [f"## {heading}", ""]
            for identifier in sorted(self.record_paths[folder], key=int):
                lines.append(f"- [{heading} {identifier}]({folder}/{identifier}.md) · [raw JSON]({folder}/{identifier}.json)")
            lines.append("")
        (self.output / "README.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", help="public owner/repository")
    parser.add_argument("--output", type=Path, default=Path("repository-archive"))
    parser.add_argument("--max-asset-bytes", type=int, default=1024 * 1024 * 1024)
    parser.add_argument("--max-total-bytes", type=int, default=4 * 1024 * 1024 * 1024)
    args = parser.parse_args()
    if not REPO_RE.fullmatch(args.repository) or ".." in args.repository:
        parser.error("Use owner/repository, not a URL or path")
    if args.max_asset_bytes <= 0 or args.max_total_bytes <= 0:
        parser.error("Size budgets must be positive")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        try:
            token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass
    return Archive(Client(token), args.repository, args.output, args.max_asset_bytes, args.max_total_bytes).run()


if __name__ == "__main__":
    sys.exit(main())
