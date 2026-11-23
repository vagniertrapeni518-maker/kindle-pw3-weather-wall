# generated: true
"""Deploy WeatherWall scriptlet, worker, and config to a jailbroken Kindle USB partition (Unix LF enforced)."""
import argparse
import shutil
import sys
from pathlib import Path


def write_lf(dst: Path, text: str) -> None:
    data = text.replace("\r\n", "\n").encode("utf-8")
    dst.write_bytes(data)
    if b"\r" in dst.read_bytes():
        sys.exit(f"ERROR: CR byte detected in {dst}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drive", required=True, help="Mounted Kindle root path, e.g. D:/ or /media/user/Kindle")
    parser.add_argument("--config", type=Path, required=True, help="Path to local filled config file")
    parser.add_argument("--clean-jailbreak-tmp", action="store_true", help="Remove fill_disk/ and winterbreak2/ after OTA block is verified")
    args = parser.parse_args()

    root = Path(args.drive)
    docs_dir = root / "documents"
    if not docs_dir.is_dir():
        sys.exit(f"ERROR: {docs_dir} not found.")
    if not args.config.is_file():
        sys.exit(f"ERROR: Config file not found: {args.config}")

    cfg_text = args.config.read_text(encoding="utf-8")
    if "YOUR_SERVER_IP" in cfg_text or "REPLACE_WITH_" in cfg_text:
        sys.exit("ERROR: Config file still contains placeholder text. Fill real server URL and token first.")

    repo_root = Path(__file__).resolve().parent.parent
    kindle_dir = repo_root / "kindle"
    wall_dir = root / "weather-wall"
    (wall_dir / "cache").mkdir(parents=True, exist_ok=True)

    write_lf(docs_dir / "03-WeatherWall.sh", (kindle_dir / "03-WeatherWall.sh").read_text(encoding="utf-8"))
    write_lf(wall_dir / "wall_worker.sh", (kindle_dir / "wall_worker.sh").read_text(encoding="utf-8"))
    write_lf(wall_dir / "config", cfg_text)

    if args.clean_jailbreak_tmp:
        for tmp in ("fill_disk", "winterbreak2"):
            p = root / tmp
            if p.is_dir():
                shutil.rmtree(p)
                print(f"Removed temporary folder: {p}")

    print("Deployed successfully. Eject Kindle safely, connect to Wi-Fi, and tap 03-WeatherWall in your library.")


if __name__ == "__main__":
    main()
