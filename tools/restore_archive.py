#!/usr/bin/env python3
"""Reconstruct archived attachments and verify their original SHA-256 hashes."""
import argparse
import hashlib
import json
from pathlib import Path
import sys


def restore(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output == root or output.is_relative_to(root):
        raise ValueError("Use a separate output directory, outside the archive")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=True)
    restored, failed = 0, 0
    for entry in manifest["assets"].values():
        if entry["status"] != "saved":
            failed += 1
            continue
        filename = entry.get("filename") or Path(entry["path"]).name
        if filename != Path(filename).name or filename in {"", ".", ".."}:
            raise ValueError("Unsafe filename in manifest")
        target = output / filename
        temporary = output / (filename + ".restoring")
        digest, size = hashlib.sha256(), 0
        try:
            if not target.resolve().is_relative_to(output) or not temporary.resolve().is_relative_to(output):
                raise ValueError("Output path escapes restore directory")
            with temporary.open("wb") as destination:
                for piece in entry.get("parts") or [{"path": entry["path"]}]:
                    source = (root / piece["path"]).resolve()
                    if not source.is_relative_to(root / "assets"):
                        raise ValueError("Unsafe source path in manifest")
                    part_digest, part_size = hashlib.sha256(), 0
                    with source.open("rb") as stream:
                        for data in iter(lambda: stream.read(1024 * 1024), b""):
                            destination.write(data)
                            digest.update(data)
                            part_digest.update(data)
                            size += len(data)
                            part_size += len(data)
                    if piece.get("sha256") and (part_digest.hexdigest() != piece["sha256"] or part_size != piece["bytes"]):
                        raise ValueError("Part checksum/size mismatch")
            if digest.hexdigest() != entry["sha256"] or size != entry["bytes"]:
                raise ValueError("Original file checksum/size mismatch")
            temporary.replace(target)
            restored += 1
        except (OSError, ValueError) as error:
            print(f"Failed {filename}: {error}", file=sys.stderr)
            failed += 1
        finally:
            if temporary.exists():
                temporary.unlink()
    print(f"Restored and verified {restored} files; {failed} failures")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    sys.exit(restore(args.archive, args.output))
