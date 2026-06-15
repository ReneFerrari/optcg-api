"""The DON resolution audit must not crash when its optional PDF image map is
absent. data/don_image_map.json is a gitignored one-off curation artifact, so
CI never has it; before this guard the audit FileNotFoundError'd on every weekly
run (non-fatal via continue-on-error, but it flagged the step red each time).
"""

import json

from scripts.audit_don_resolution import load_pdf_image_map


def test_returns_empty_when_map_absent(tmp_path):
    assert load_pdf_image_map(tmp_path) == []


def test_loads_map_when_present(tmp_path):
    (tmp_path / "don_image_map.json").write_text(
        json.dumps([{"filename": "a.png", "set_id": "OP01"}]), encoding="utf-8"
    )
    assert load_pdf_image_map(tmp_path) == [{"filename": "a.png", "set_id": "OP01"}]
