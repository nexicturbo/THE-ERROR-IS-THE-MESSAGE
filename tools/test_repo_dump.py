"""Network-free regression tests: python -m unittest discover -s tools -v."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.request import Request

from repo_dump import API, Archive, Client, SafeRedirect, attachment_urls, is_attachment


class Response(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}


class Open:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def open(self, request, timeout=0):
        self.requests.append(request)
        data, headers = self.responses[request.full_url]
        return Response(data, headers)


class FakeClient:
    def __init__(self, asset=b"image", fail=False):
        self.asset, self.fail, self.downloads = asset, fail, 0
        self.url = "https://github.com/user-attachments/assets/image-id"

    def json(self, path):
        if path.endswith("/pulls/2"):
            return {"number": 2, "title": "Closed PR", "state": "closed", "body": "Pull text"}
        return {"private": False, "name": "fixture"}

    def pages(self, path):
        if "/issues?" in path:
            return [{"number": 1, "state": "closed", "title": "Closed issue", "body": self.url},
                    {"number": 2, "pull_request": {}}]
        if "/pulls?" in path:
            return [{"number": 2}]
        if path.endswith("/releases"):
            return [{"id": 3, "tag_name": "v1", "body": "release note"}]
        if path.endswith("/tags"):
            return [{"name": "v1", "commit": {"sha": "abc"}}]
        if path.endswith("/releases/3/assets"):
            return [{"name": "release.bin", "browser_download_url": "https://github.com/o/r/releases/download/v1/release.bin",
                     "url": API + "/repos/o/r/releases/assets/4"}]
        if path.endswith("/reviews"):
            return [{"body": "Review summary", "state": "COMMENTED"}]
        if path.endswith("/comments"):
            return [{"id": 5, "body": "Full comment " + self.url, "in_reply_to_id": 4}]
        if path.endswith("/files"):
            return [{"filename": "code.py", "patch": "+ fixed"}]
        raise AssertionError(path)

    def open(self, url, binary=False):
        self.downloads += 1
        if self.fail:
            raise RuntimeError("GitHub HTTP 404")
        return Response(self.asset, {"Content-Type": "image/png", "Content-Length": str(len(self.asset))})


class ExportTests(unittest.TestCase):
    def test_markdown_html_video_and_nested_attachment_extraction(self):
        one = "https://github.com/user-attachments/files/123/code.json"
        two = "https://github.com/user-attachments/assets/abc"
        old = "https://user-images.githubusercontent.com/1/2.png"
        body = {"body": f"[download]({one})", "comments": [{"body": f'<video src="{two}"></video> <img src="{old}">'}]}
        self.assertEqual(attachment_urls(body), {one, two, old})
        self.assertFalse(is_attachment("https://github.com.evil.test/user-attachments/assets/a"))
        self.assertFalse(is_attachment("http://github.com/user-attachments/assets/a"))
        self.assertFalse(is_attachment("https://github.com:8443/user-attachments/assets/a"))
        self.assertFalse(is_attachment("https://[malformed"))
        self.assertFalse(is_attachment("https://github.com:broken/user-attachments/assets/a"))

    def test_pagination_follows_next_not_count_or_first_page_only(self):
        first = API + "/repos/o/r/issues?state=all&per_page=100"
        second = API + "/repos/o/r/issues?page=2"
        opener = Open({first: (b'[{"number":1}]', {"Link": f'<{second}>; rel="next"'}),
                       second: (b'[{"number":2}]', {})})
        self.assertEqual(Client(opener=opener).pages("/repos/o/r/issues?state=all"), [{"number": 1}, {"number": 2}])
        self.assertEqual(len(opener.requests), 2)

    def test_token_only_sent_to_api_and_removed_from_redirect(self):
        asset = "https://github.com/user-attachments/assets/a"
        opener = Open({asset: (b"x", {})})
        Client("secret", opener).open(asset, binary=True)
        self.assertIsNone(opener.requests[0].get_header("Authorization"))
        request = Request(API + "/repos/o/r/releases/assets/1", headers={"Authorization": "Bearer secret"})
        redirect = SafeRedirect().redirect_request(request, None, 302, "Found", {}, "https://release-assets.githubusercontent.com/x")
        self.assertIsNone(redirect.get_header("Authorization"))
        legacy = SafeRedirect().redirect_request(request, None, 302, "Found", {}, "https://github-production-user-asset-6210df.s3.amazonaws.com/x")
        self.assertIsNone(legacy.get_header("Authorization"))
        with self.assertRaises(RuntimeError):
            SafeRedirect().redirect_request(request, None, 302, "Found", {}, "https://evil.test/x")
        with self.assertRaises(RuntimeError):
            SafeRedirect().redirect_request(request, None, 302, "Found", {}, "https://other-bucket.s3.amazonaws.com/x")

    def test_full_scope_and_reuse_checksums_without_redownload(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            first = Archive(client, "o/r", directory, 1000, 10000)
            self.assertEqual(first.run(), 0)
            self.assertEqual(first.manifest["counts"], {"issues": 1, "pull_requests": 1, "releases": 1, "tags": 1})
            self.assertEqual(client.downloads, 2)  # one attachment reused in all conversations + release
            root = Path(directory)
            pull = json.loads((root / "pulls/2.json").read_text())
            self.assertEqual(pull["review_comments"][0]["in_reply_to_id"], 4)
            self.assertIn("../assets/", (root / "issues/1.md").read_text())
            client.downloads = 0
            second = Archive(client, "o/r", directory, 1000, 10000)
            self.assertEqual(second.run(), 0)
            self.assertEqual(client.downloads, 0)
            self.assertTrue(all(a["reused"] for a in second.assets.values()))
            first_asset = next(iter(second.assets.values()))
            (root / first_asset["path"]).write_bytes(b"corrupt")
            third = Archive(client, "o/r", directory, 1000, 10000)
            self.assertEqual(third.run(), 0)
            self.assertEqual(client.downloads, 1)

    def test_missing_download_never_reports_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Archive(FakeClient(fail=True), "o/r", directory, 1000, 10000)
            self.assertEqual(archive.run(), 2)
            manifest = json.loads((Path(directory) / "manifest.json").read_text())
            self.assertFalse(manifest["complete"])
            self.assertEqual(len(manifest["errors"]), 2)
            self.assertIn("INCOMPLETE", (Path(directory) / "README.md").read_text())

    def test_oversized_asset_is_failure_not_silent_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Archive(FakeClient(asset=b"123456"), "o/r", directory, 5, 100)
            self.assertEqual(archive.run(), 2)
            self.assertFalse(list(Path(directory).rglob("*.part")))
            self.assertTrue(all(a["status"] == "failed" for a in archive.assets.values()))

    def test_json_code_attachment_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            url = "https://github.com/user-attachments/files/1/example.json"
            client = Client(opener=Open({url: (b'{"example":true}', {"Content-Type": "application/json"})}))
            archive = Archive(client, "o/r", directory, 1000, 1000)
            archive.download(url, "issues/1")
            self.assertEqual(archive.assets[url]["status"], "saved")

    def test_truncated_stream_is_not_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            url = "https://github.com/user-attachments/assets/a"
            client = Client(opener=Open({url: (b"short", {"Content-Length": "100"})}))
            archive = Archive(client, "o/r", directory, 1000, 1000)
            archive.download(url, "issues/1")
            self.assertEqual(archive.assets[url]["status"], "failed")
            self.assertFalse(list(Path(directory).rglob("*.part")))

    def test_different_repository_cannot_reuse_same_output(self):
        with tempfile.TemporaryDirectory() as directory:
            Archive(FakeClient(), "o/r", directory, 1000, 10000).run()
            with self.assertRaises(ValueError):
                Archive(FakeClient(), "other/repository", directory, 1000, 10000)

    def test_private_repository_is_rejected_before_archiving(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            client.json = lambda path: {"private": True}
            archive = Archive(client, "o/r", directory, 1000, 10000)
            self.assertEqual(archive.run(), 2)
            self.assertEqual(client.downloads, 0)
            self.assertFalse((Path(directory) / "metadata/repository.json").exists())

    def test_stream_without_content_length_still_enforces_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            url = "https://github.com/user-attachments/assets/a"
            client = Client(opener=Open({url: (b"abcdef", {})}))
            archive = Archive(client, "o/r", directory, 5, 1000)
            archive.download(url, "issues/1")
            self.assertEqual(archive.assets[url]["status"], "failed")
            self.assertFalse(list(Path(directory).rglob("*.part")))


if __name__ == "__main__":
    unittest.main()
