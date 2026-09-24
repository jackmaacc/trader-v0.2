import json
from datetime import datetime, timezone, timedelta
from streamlit.testing.v1 import AppTest


def app(root):
    return AppTest.from_string('from pathlib import Path\nfrom trader_engine.ui.project_operations import render_project_operations\nrender_project_operations(Path('+repr(str(root))+'))').run()


def test_missing_trial_cannot_claim_elapsed_days(tmp_path):
    result=app(tmp_path)
    assert not result.exception
    assert any('No elapsed time' in r.value for r in result.info)
    assert any('Live trading is disabled' in r.value for r in result.warning)


def test_stale_qualified_flag_does_not_announce_qualification(tmp_path):
    p=tmp_path/'paper_trial';p.mkdir()
    (p/'status.json').write_text(json.dumps(dict(checked_at=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),status='recording',continuous_seconds=30*86400,required_seconds=30*86400,operational_qualified=True)))
    result=app(tmp_path)
    assert not result.exception
    assert not result.success
    assert any('observer is stale' in r.value for r in result.warning)


def test_fresh_qualification_flag_cannot_override_zero_elapsed_time(tmp_path):
    p=tmp_path/'paper_trial';p.mkdir()
    (p/'status.json').write_text(json.dumps(dict(checked_at=datetime.now(timezone.utc).isoformat(),status='observing',continuous_seconds=0,required_seconds=1,operational_qualified=True)))
    result=app(tmp_path)
    assert not result.exception and not result.success
