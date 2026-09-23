import pandas as pd,pytest
from trader_engine.data.alpaca_intraday import download_minutes

def test_read_only_pagination_and_explicit_contract(tmp_path):
 calls=[]
 def fetch(kind,p):
  calls.append((kind,p))
  if kind=='calendar':return [{'date':'2026-09-01','open':'09:30','close':'16:00'}]
  t='2026-09-01T13:30:00Z' if 'page_token' not in p else '2026-09-01T13:31:00Z'
  return {'bars':{'ABC':[{'t':t,'o':100,'h':101,'l':99,'c':100,'v':1000}]},'next_page_token':'next' if 'page_token' not in p else None}
 manifest=download_minutes(['ABC'],'2026-09-01','2026-09-01',tmp_path/'data',fetcher=fetch)
 assert len(calls)==3 and all(p['feed']=='sip' and p['adjustment']=='raw' for k,p in calls if k=='bars')
 assert len(pd.read_parquet(tmp_path/'data/ABC.parquet'))==2
 assert set(manifest['hashes'])=={'ABC.parquet','calendar.csv'}
 with pytest.raises(FileExistsError):download_minutes(['ABC'],'2026-09-01','2026-09-01',tmp_path/'data',fetcher=fetch)

def test_repeated_pagination_refuses_incomplete_dataset(tmp_path):
 def fetch(kind,p):
  if kind=='calendar':return [{'date':'2026-09-01','open':'09:30','close':'16:00'}]
  return {'bars':{},'next_page_token':'loop'}
 with pytest.raises(ValueError,match='Repeated'):download_minutes(['ABC'],'2026-09-01','2026-09-01',tmp_path/'data',fetcher=fetch)
 assert not (tmp_path/'data').exists()

@pytest.mark.parametrize('symbols',[[],['../BAD'],['ABC','ABC']])
def test_invalid_symbols_rejected_before_request(symbols,tmp_path):
 def fetch(*args):pytest.fail('No request permitted')
 with pytest.raises(ValueError):download_minutes(symbols,'2026-09-01','2026-09-01',tmp_path/'data',fetcher=fetch)
