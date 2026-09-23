from datetime import datetime,timezone,timedelta
import importlib.util
from pathlib import Path
import json
import subprocess
import sys

root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('paper_loop',root/'scripts/paper_until_close.py')
loop=importlib.util.module_from_spec(spec);spec.loader.exec_module(loop)

def test_close_window_freezes_session_and_reserves_two_minutes():
    close=datetime(2026,9,23,20,tzinfo=timezone.utc)
    def clock(now,close=close,is_open=True):return {'is_open':is_open,'timestamp':now.isoformat(),'next_close':close.isoformat()}
    now=close-timedelta(minutes=3)
    assert loop.entry_window_open(clock(now),close,now)
    cutoff=close-timedelta(minutes=2)
    assert not loop.entry_window_open(clock(cutoff),close,cutoff)
    assert not loop.entry_window_open(clock(now,is_open=False),close,now)
    assert not loop.entry_window_open(clock(now,close+timedelta(days=1)),close,now)
    assert not loop.entry_window_open(clock(now),close,cutoff)

def test_quote_gates_future_stale_crossed_nonfinite_and_wide():
    now=datetime.now(timezone.utc)
    for bid,ask,age in [('100','100.01',-1),('100','100.01',6),('101','100',1),('NaN','100',1),('100','101',1)]:
        q={'bp':bid,'ap':ask,'t':(now-timedelta(seconds=age)).isoformat()}
        assert loop.qualified_quote(q,now) is None
    q={'bp':'100','ap':'100.01','t':(now-timedelta(seconds=1)).isoformat()}
    limit,stop=loop.qualified_quote(q,now)
    assert limit>100 and 0<stop<limit

def test_loop_preview_never_starts_trading():
    result=subprocess.run([sys.executable,str(root/'scripts/paper_until_close.py')],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    value=json.loads(result.stdout)
    assert value['mode']=='preview_no_orders' and value['max_positions']==1


def test_larger_paper_profile_preview_explicitly_disables_session_cutoff():
    result=subprocess.run([sys.executable,str(root/'scripts/paper_until_close.py'),
        '--target-notional','15000','--max-position-notional','15000',
        '--position-equity-fraction','0.20','--no-session-loss-limit'],
        capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    value=json.loads(result.stdout)
    assert value['mode']=='preview_no_orders'
    assert value['target_notional']=='15000' and value['max_position_notional']=='15000'
    assert value['max_position_equity_fraction']=='0.20'
    assert value['session_loss_cutoff'] is None and value['max_positions']==1

def test_session_cutoff_stays_enabled_without_explicit_override():
    result=subprocess.run([sys.executable,str(root/'scripts/paper_until_close.py')],capture_output=True,text=True,timeout=10)
    assert result.returncode==0
    assert json.loads(result.stdout)['session_loss_cutoff']=='1000'
