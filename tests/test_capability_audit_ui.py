from pathlib import Path
from streamlit.testing.v1 import AppTest


def test_missing_capabilities_render_without_authorizing(tmp_path):
    result=AppTest.from_string('from pathlib import Path\nfrom trader_engine.ui.capability_audit import render_capability_audit\nrender_capability_audit(Path('+repr(str(tmp_path))+'))').run()
    assert not result.exception
    assert not result.success
    assert any('local evidence only' in x.value for x in result.caption)
    assert len(result.dataframe)==1
    assert set(result.dataframe[0].value['Market'])=={'Stocks / ETFs','Options','Crypto','Futures','Forex'}


def test_bad_snapshot_does_not_crash_or_authorize(tmp_path):
    evidence=tmp_path/'capability_evidence';evidence.mkdir()
    (evidence/'paper_account_bad.json').write_text('{invalid')
    result=AppTest.from_string('from pathlib import Path\nfrom trader_engine.ui.capability_audit import render_capability_audit\nrender_capability_audit(Path('+repr(str(tmp_path))+'))').run()
    assert not result.exception and not result.success
