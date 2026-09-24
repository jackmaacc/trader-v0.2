import importlib.util
from pathlib import Path
import sys
import json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from plus_research_service import ResearchService

class Client:
    def __init__(self): self.calls=[]
    def result(self, feed='sip'): return dict(records=[],complete=True,errors=[],provenance={'feed':feed})
    def bars(self,*args,**kwargs):
        self.calls.append(('bars',args,kwargs));return self.result()
    def latest_quotes(self,*args): return self.result()
    def option_chain(self,*args,**kwargs): return self.result('opra')
    def option_contracts(self,*args,**kwargs): return self.result(None)

def test_cycle_records_reasons_without_order_methods(tmp_path):
    client=Client();svc=ResearchService(client,tmp_path/'out',tmp_path)
    status=svc.cycle()
    assert status['broker_execution_enabled'] is False
    rows=json.loads((tmp_path/'out/decisions.json').read_text())['records']
    assert len(rows)==10 and all(r['decision']=='data_blocked' for r in rows)
    assert client.calls[0][2]['adjustment']=='raw'
    options=json.loads((tmp_path/'out/options.json').read_text())
    assert all('assessment_truncated' in x for x in options['sources'])


def test_stream_collection_sample_balances_underlyings():
    from plus_research_service import order_contract_sample
    groups=[('SPY',[dict(symbol='far',strike_price='200'),dict(symbol='near',strike_price='100')]),('QQQ',[dict(symbol='qqq',strike_price='50')])]
    quotes=dict(records=[dict(symbol='SPY',bp=99,ap=101),dict(symbol='QQQ',bp=49,ap=51)])
    assert [r['symbol'] for r in order_contract_sample(groups,quotes)]==['near','qqq','far']
