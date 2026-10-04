"""
Directory Scanner - Creates CSV output for second-level directory names only
Usage: python albumfinder.py [start_path] [output_file]
"""

import csv
import sys
from datetime import datetime
from pathlib import Path


def scan_directories(start_path: Path, output_file: Path) -> None:
    print(f"Scanning directory: {start_path}")
    print(f"Output file: {output_file}")

    first_level = sorted(
        [d for d in start_path.iterdir() if d.is_dir()],
        key=lambda d: d.name.lower()
    )

    if not first_level:
        print(f"No subdirectories found in {start_path}")
        return

    results = []

    for first_dir in first_level:
        print(f"Processing: {first_dir.name}")

        try:
            second_level = sorted(
                [d for d in first_dir.iterdir() if d.is_dir()],
                key=lambda d: d.name.lower()
            )
        except PermissionError:
            print(f"  Skipped (permission denied)")
            continue

        for second_dir in second_level:
            print(f"  Found: {second_dir.name}")
            created = datetime.fromtimestamp(second_dir.stat().st_ctime)
            results.append({
                "First_Level_Directory": first_dir.name,
                "Second_Level_Directory": second_dir.name,
                "Created_Date": created.strftime("%Y-%m-%d %H:%M:%S"),
                # "Modified_Date": datetime.fromtimestamp(second_dir.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                # "Full_Path": str(second_dir),
                # "Relative_Path": str(second_dir.relative_to(start_path)),
            })

    if not results:
        print("\nNo second-level directories found")
        return

    fieldnames = ["First_Level_Directory", "Second_Level_Directory", "Created_Date"]
    with output_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    print(f"\nScan complete!")
    print(f"Found {len(results)} second-level directories")
    print(f"Results saved to: {output_file}")

    print("\nSummary:")
    counts: dict[str, int] = {}
    for row in results:
        counts[row["First_Level_Directory"]] = counts.get(row["First_Level_Directory"], 0) + 1
    for artist, count in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {artist}: {count} subdirectories")


if __name__ == "__main__":
    start_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    output_file = (
        Path(sys.argv[2])
        if len(sys.argv) > 2
        else Path("albums.csv")
    )

    if not start_path.is_dir():
        print(f"Error: '{start_path}' is not a valid directory")
        sys.exit(1)

    scan_directories(start_path, output_file)
