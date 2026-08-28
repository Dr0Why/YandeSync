from pathlib import Path

from yande_sync.reporter import print_download_plan


def test_download_plan_reports_difference_and_planned_files(tmp_path, capsys):
    complete = tmp_path / "tag" / "1.jpg"
    complete.parent.mkdir()
    complete.write_bytes(b"ok")
    recorded = [
        {
            "file_name": "1.jpg", "file_size": 2, "file_ext": "jpg",
            "status": "downloaded", "relative_path": str(Path("tag") / complete.name),
            "folder_name": "tag",
        },
        {
            "file_name": "2.png", "file_size": 2048, "file_ext": "png",
            "status": "new", "relative_path": None, "folder_name": "tag",
        },
        {
            "file_name": "3.webp", "file_size": 4096, "file_ext": "webp",
            "status": "failed", "relative_path": None, "folder_name": "tag",
        },
    ]

    print_download_plan(recorded, recorded[1:])

    output = capsys.readouterr().out
    assert "相差: 2" in output
    assert "计划下载: 2" in output
    assert "- 2.png | 2.00 KiB | PNG" in output
    assert "- 3.webp | 4.00 KiB | WEBP" in output


def test_download_plan_reports_no_work(capsys):
    print_download_plan([], [])
    output = capsys.readouterr().out
    assert "相差: 0" in output
    assert "计划下载: 0" in output
    assert output.endswith("- 无\n")
