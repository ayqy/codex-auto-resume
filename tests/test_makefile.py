from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "scripts" / "codex_token_usage.py"


def test_make_yesterday_passes_yesterday_range_and_detail_file(tmp_path):
    captured_args = tmp_path / "captured-args.json"
    detail_file = tmp_path / "yesterday.txt"
    python_spy = tmp_path / "python_spy.py"
    python_spy.write_text(
        "import json, sys\n"
        f"open({str(captured_args)!r}, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "make",
            "yesterday",
            f"PYTHON={sys.executable} {python_spy}",
            f"F={detail_file}",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(captured_args.read_text(encoding="utf-8")) == [
        str(SCRIPT_PATH),
        "-y",
        "-f",
        str(detail_file),
    ]
