# Archive this repository from a phone

The **Repository archive** Actions workflow exports public repository issues, pull requests, release notes, uploaded media and release assets into the **`repository-archive` branch in the same repository**. The main/source branch stays intact. No server, subscription or local installation is needed when using Actions.

**One-time owner installation:** this contribution includes the workflow as [repository-archive.workflow.yml](repository-archive.workflow.yml). Copy its contents to `.github/workflows/repository-archive.yml` on the default branch. The contributor's current GitHub credential cannot register Actions workflows, so the executable and local tests can be demonstrated directly, but the Actions run requires this owner installation. The template does not run automatically merely by merging this contribution.

1. Open this repository on GitHub in your phone's browser. If testing a fork, open that fork.
2. Open **Actions → Repository archive → Run workflow**. The workflow must already exist on the repository's default branch. GitHub may first ask the repository owner to enable Actions on a fork.
3. Leave `source_repository` empty for this repository, or enter a public `owner/repository` such as `attogram/THE-ERROR-IS-THE-MESSAGE`.
4. Run it. When finished, follow the archive link in the run's Summary. Open `archive/README.md` on the `repository-archive` branch.
5. A red run means the archive is incomplete. Open `archive/manifest.json` for the failed sources or attachments. Correct the cause and rerun; a red run is never described as a successful complete export.

Repository settings or organization policy must permit Actions to write repository contents. A protected archive branch may need owner configuration. The workflow uses the existing repository-scoped `GITHUB_TOKEN`; do not add a personal access token. Public standard GitHub-hosted runner usage is intended; no paid service or purchase is configured.

## What is preserved

- All open and closed **issues**, with each full issue comment.
- All open and closed **pull requests**, general comments, review summaries, inline review comments/replies (`in_reply_to_id` and original context retained), and changed-file metadata/available patches.
- **Releases**, full notes, paginated release assets/binaries, and repository tag metadata with commit references.
- The repository's main **README**, its original source/API metadata, and uploaded media referenced there.
- GitHub-uploaded images, video, audio, PDFs and other files linked in those records, including Markdown, HTML `src` and bare attachment URLs. Legacy GitHub attachment hosts are recognized.
- Raw JSON alongside readable Markdown. Readable copies point to the downloaded local assets; original URLs remain in raw JSON and the manifest.
- A manifest with source-to-file mapping, SHA-256 hashes, byte sizes, source records, snapshot times and failures. On reruns, cached assets are reused only after their hashes are checked; corrupted or previously failed assets are downloaded again.

Repeated attachment URLs are downloaded once. Every paginated API collection follows GitHub's next-page links, including comments, reviews and release assets. PRs returned by the issues endpoint are separated to avoid double-counting them as issues.

Attachment discovery scans conversation bodies, including inline links, rather than code patches containing test fixtures. An explicit all-x example such as `https://github.com/user-attachments/assets/xxxx` is retained in the source text and listed in `manifest.ignored_urls`; it is not an uploaded file. Other missing attachment URLs remain errors.

The generated `.gitattributes` disables line-ending conversion for archived assets, including text/code attachments, so Git checkout settings do not silently change their bytes.

## Local command

Python 3.10+ and an internet connection are sufficient. An authenticated GitHub CLI session or `GH_TOKEN`/`GITHUB_TOKEN` environment variable avoids the low anonymous API rate limit. Tokens are never written to the output or sent to attachment/CDN hosts; authorization is restricted to `api.github.com` and removed on CDN redirects.

```sh
python3 tools/repo_dump.py attogram/THE-ERROR-IS-THE-MESSAGE --output repository-archive
python3 -m unittest discover -s tools -v
```

Exit code `0`: every fetched record and recognized attachment succeeded within the scope below. Exit code `2`: incomplete; inspect the manifest. Standard argument errors also exit nonzero. An archive directory cannot be reused for a different source repository.

## Explicit scope and practical limits

This is a sequential API snapshot: activity added during a run may arrive in the next run. It cannot recover deleted data, inaccessible/draft releases, or content hidden from the current token. Native **GitHub Discussions**, project boards, wikis, Actions artifacts/logs, individual reactions, release source-code ZIP/tar snapshots, and full git history are outside this issue/PR/release exporter. The repository already stores its own git history. API-provided changed-file patches can be truncated or absent for large/binary diffs; raw file metadata is retained, but this is not a replacement for a git clone.

Externally hosted links are retained as links and are not fetched. Only recognized GitHub-uploaded attachments and release assets are downloaded. SVG/HTML/code attachments are archived as inert files, never executed by the exporter. Treat archived content as untrusted when viewing it in other programs.

The default per-file download budget is **1 GiB** and the default per-run budget is **4 GiB**. Files larger than **48 MiB** are preserved as ordered binary chunks below GitHub's normal 100 MiB file limit. Each part and the entire original file have independent SHA-256 checksums. A large media link points to reconstruction instructions; download/clone the archive and run `python3 tools/restore_archive.py PATH_TO_ARCHIVE OUTPUT_DIRECTORY` to reconstruct its playable original. This avoids LFS or paid external storage. Smaller files remain directly viewable where GitHub supports their format.

Oversized, missing, inaccessible, truncated or login-page responses produce a failed manifest entry and nonzero exit, not silent success. The CLI supports `--max-asset-bytes` and `--max-total-bytes`. Repository owners should consider repository growth before scheduling large archives; this workflow is manual only. Very large repositories may also hit GitHub's total push-size limit and need a separate storage policy.

Reruns refresh the currently visible records and index. Previously archived files may remain on the archive branch for preservation; `manifest.records` identifies the records fetched in this particular run. A source mismatch stops the workflow rather than mixing two repositories' archives.

## Acceptance check for issue 60

Run against `attogram/THE-ERROR-IS-THE-MESSAGE`. Inspect `archive/issues/60.md`, the corresponding raw JSON, and its asset entries in `manifest.json`. The owner's PDF specification and image/audio examples should have saved files and checksums. Inspect the closed issues, PR reviews and release sections as well. On a repository with no releases, an empty release list is reported honestly; tests separately exercise release-file preservation.
