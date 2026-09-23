from pathlib import Path
import json
import os
import subprocess
import sys
import pytest
from trader_engine.execution.journal import ReservedJournal
from trader_engine.execution.lifecycle import PaperExecutor,TradePlan,EntryBlocked
from test_paper_lifecycle import FakeBroker


def test_real_reserved_journal_full_after_buy_still_flattens(tmp_path):
    broker=FakeBroker()
    with ReservedJournal(tmp_path/'journal',capacity=2) as journal:
        engine=PaperExecutor(broker,journal,polls=1,sleep=lambda _:None,poll_seconds=0)
        result=engine.execute(TradePlan.create('SPY',10,'100'))
        assert result.status=='flat' and result.storage_failed
        assert broker.position==0 and len(broker.sent)==2
        with pytest.raises(EntryBlocked):engine.execute(TradePlan.create('QQQ',1,'100'))


def test_real_disk_fault_after_entry_preserves_exit(tmp_path,monkeypatch):
    broker=FakeBroker()
    with ReservedJournal(tmp_path/'journal') as journal:
        engine=PaperExecutor(broker,journal,polls=1,sleep=lambda _:None,poll_seconds=0)
        original=broker.submit
        def submit(payload):
            response=original(payload)
            if payload['side']=='buy':
                def no_space(*args,**kwargs):raise OSError(28,'No space left on device')
                monkeypatch.setattr(journal,'_write_all',no_space)
            return response
        broker.submit=submit
        result=engine.execute(TradePlan.create('SPY',10,'100'))
        assert result.status=='flat' and result.storage_failed and broker.position==0
        assert len(broker.sent)==2


def test_cli_default_is_offline_and_uses_meaningful_size():
    root=Path(__file__).resolve().parents[1]
    env={k:v for k,v in os.environ.items() if k not in {'APCA_API_KEY_ID','APCA_API_SECRET_KEY'}}
    result=subprocess.run([sys.executable,str(root/'scripts/paper_execution.py'),'--symbol','SPY','--limit-price','100','--stop-price','99'],env=env,capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    output=json.loads(result.stdout)
    assert output['mode']=='offline_preview_no_orders'
    assert output['quantity']==75 and output['notional']=='7500'


def test_cli_explicit_execution_requires_new_journal_before_credentials():
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run([sys.executable,str(root/'scripts/paper_execution.py'),'--symbol','SPY','--limit-price','100','--stop-price','99','--execute-paper'],capture_output=True,text=True,timeout=10)
    assert result.returncode==2 and 'NEW --journal' in result.stdout
