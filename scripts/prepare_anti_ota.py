# generated: true
"""Fill Kindle USB storage down to ~70 MB free to prevent silent OTA updates before jailbreak."""
import argparse
import shutil
import sys
from pathlib import Path

WRITE_BLOCK = 4 * 1024 * 1024
CHUNK_MB = 500


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drive", required=True, help="Mounted Kindle root path, e.g. D:/ or /media/user/Kindle")
    parser.add_argument("--keep-mb", type=int, default=70, help="Free space to leave (KindleModding safe zone: 50-90 MB)")
    args = parser.parse_args()

    root = Path(args.drive)
    if not (root / "documents").is_dir():
        sys.exit(f"ERROR: {root / 'documents'} not found; aborting to avoid touching wrong drive.")

    update_hits = [
        p.name
        for p in root.iterdir()
        if p.is_file() and (p.name.lower().endswith(".bin") or p.name.lower().endswith(".partial"))
    ]
    if update_hits:
        sys.exit(f"ERROR: Existing OTA file(s) detected on root: {', '.join(update_hits)}")

    fill_dir = root / "fill_disk"
    fill_dir.mkdir(exist_ok=True)

    free_bytes = shutil.disk_usage(root).free
    need_bytes = free_bytes - args.keep_mb * 1024 * 1024
    if need_bytes <= 0:
        print(f"Free space is already {free_bytes / (1024 * 1024):.1f} MB <= {args.keep_mb} MB.")
        return

    zero_buf = b"\x00" * WRITE_BLOCK
    remaining = need_bytes
    written = 0
    idx = 1
    while remaining > 0:
        part_size = min(remaining, CHUNK_MB * 1024 * 1024)
        part_path = fill_dir / f"fill_{idx:02d}.dat"
        while part_path.exists():
            idx += 1
            part_path = fill_dir / f"fill_{idx:02d}.dat"
        with part_path.open("wb") as f:
            left = part_size
            while left > 0:
                step = min(left, WRITE_BLOCK)
                f.write(zero_buf if step == WRITE_BLOCK else b"\x00" * step)
                left -= step
                written += step
                print(f"\rProgress: {written / (1024 * 1024):.0f} / {need_bytes / (1024 * 1024):.0f} MB", end="", flush=True)
        remaining -= part_size
        idx += 1
    print(f"\nDone. Final free space: {shutil.disk_usage(root).free / (1024 * 1024):.1f} MB")


if __name__ == "__main__":
    main()
