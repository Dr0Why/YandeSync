from __future__ import annotations

from pathlib import Path


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def print_download_plan(recorded_rows, planned_rows) -> None:
    differences = []
    for row in recorded_rows:
        expected = Path(str(row["folder_name"])) / str(row["file_name"])
        locally_complete = (
            row["status"] == "downloaded"
            and bool(row["relative_path"])
            and Path(str(row["relative_path"])) == expected
        )
        if not locally_complete:
            differences.append(row)

    print(f"相差: {len(differences)}")
    print(f"计划下载: {len(planned_rows)}")
    if not planned_rows:
        print("- 无")
        return
    for row in planned_rows:
        print(f"- {row['file_name']} | {format_size(row['file_size'])} | "
              f"{row['file_ext'].upper()}")
