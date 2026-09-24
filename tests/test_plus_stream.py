from datetime import datetime,timedelta,timezone
import importlib.util
import json
from pathlib import Path
import pytest
from trader_engine.data.plus_stream import StreamState,StreamFailure,QUOTE_SYMBOLS,decode_frame
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('plus_stream_service',ROOT/'scripts/plus_stream_service.py')
service=importlib.util.module_from_spec(spec);spec.loader.exec_module(service)
NOW=datetime(2026,9,24,14,tzinfo=timezone.utc)
def ready(state):
    state.begin_connection();state.ingest({'T':'success','msg':'authenticated'},NOW)
    ack={'T':'subscription','quotes':list(state.quote_symbols)}
    if state.feed=='sip':ack['bars']=['*']
    state.ingest(ack,NOW)
def quote(symbol='SPY',age=0):return {'T':'q','S':symbol,'bp':100,'ap':101,'bs':2,'as':3,'t':(NOW-timedelta(seconds=age)).isoformat()}
def bar(symbol='SPY',age=60):return {'T':'b','S':symbol,'o':100,'h':102,'l':99,'c':101,'v':10,'t':(NOW-timedelta(seconds=age)).isoformat()}

def test_auth_and_exact_subscription_required_before_freshness():
    state=StreamState();state.begin_connection();state.ingest(quote(),NOW)
    assert not state.quotes and state.counts['invalid']==1
    state.ingest({'T':'success','msg':'authenticated'},NOW)
    with pytest.raises(StreamFailure):state.ingest({'T':'subscription','bars':['SPY'],'quotes':list(QUOTE_SYMBOLS)},NOW)
    ready(state);state.ingest(quote(),NOW);health,latest=state.snapshot(NOW)
    assert health['status']=='streaming' and latest['quotes']['SPY']['state']=='fresh'
    state.disconnected('connection_failed');health,latest=state.snapshot(NOW)
    assert not health['authenticated'] and latest['quotes']['SPY']['state']=='retained_previous_session'

def test_invalid_future_crossed_incomplete_and_old_data():
    state=StreamState();ready(state)
    state.ingest(quote(age=-1),NOW);crossed=quote();crossed['bp']=102;state.ingest(crossed,NOW);state.ingest(bar(age=59),NOW)
    assert state.counts['invalid']==3 and not state.bars and not state.quotes
    state.ingest(quote(),NOW);state.ingest(quote(age=1),NOW)
    assert state.counts['out_of_order']==1
    health,latest=state.snapshot(NOW+timedelta(seconds=61))
    assert health['status']=='subscribed_idle' and latest['quotes']['SPY']['state']=='stale'

def test_bounded_latest_memory_and_restored_observations_are_not_live():
    state=StreamState(max_bar_symbols=2);ready(state)
    for symbol in ['A','B','C']:state.ingest(bar(symbol),NOW)
    assert list(state.bars)==['B','C'] and state.counts['evictions']==1
    _,latest=state.snapshot(NOW)
    restored=StreamState(max_bar_symbols=2);restored.restore(latest)
    assert restored.counts['restored']==2 and restored.counts['bars']==0
    ready(restored);health,latest=restored.snapshot(NOW)
    assert health['fresh_counts']['bars']==0 and latest['bars']['B']['state']=='retained_previous_session'

@pytest.mark.parametrize('value',['{}','bad','[null]','[1]'])
def test_bad_frames_rejected(value):
    with pytest.raises(StreamFailure):decode_frame(value)

def test_provider_errors_do_not_expose_raw_text():
    state=StreamState()
    with pytest.raises(StreamFailure,match='insufficient_subscription') as exc:
        state.ingest({'T':'error','code':409,'msg':'secret-token'},NOW)
    assert 'secret-token' not in str(exc.value)

def test_session_auth_once_subscription_once_and_heartbeat_without_data():
    state=StreamState()
    class Socket:
        def __init__(self):self.sent=[];self.index=0
        def send(self,payload):self.sent.append(json.loads(payload))
        def recv(self,timeout):
            self.index+=1
            events=[{'T':'success','msg':'connected'},{'T':'success','msg':'authenticated'},{'T':'subscription','bars':['*'],'quotes':list(QUOTE_SYMBOLS)}]
            return json.dumps([events[self.index-1]])
    socket=Socket();flushes=[]
    service.run_session(socket,state,'fixture-key','fixture-secret',stop=lambda:socket.index==3,flush=lambda:flushes.append(state.snapshot(NOW)[0]),now=lambda:NOW)
    assert [r['action'] for r in socket.sent]==['auth','subscribe']
    assert flushes[-1]['status']=='subscribed_idle'
    assert 'fixture-secret' not in json.dumps(flushes)

def test_missing_auth_ack_times_out():
    class Socket:
        def send(self,payload):pass
        def recv(self,timeout):raise TimeoutError()
    ticks=iter([0,0,0,11])
    with pytest.raises(StreamFailure,match='timeout'):
        service.run_session(Socket(),StreamState(),'key','secret',stop=lambda:False,flush=lambda:None,clock=lambda:next(ticks),now=lambda:NOW)

def test_opra_requires_exact_selected_quotes_and_supports_zero_bid():
    state=StreamState(feed='opra',quote_symbols=['SPY261218C00600000']);ready(state)
    q=quote('SPY261218C00600000');q['bp']=0;state.ingest(q,NOW)
    health,latest=state.snapshot(NOW)
    assert health['feed']=='opra' and health['subscription']['bars']==[]
    assert latest['quotes'][q['S']]['bid']==0
    state.ingest(bar(),NOW);assert not state.bars

def test_msgpack_decoding_and_selected_options_file(tmp_path):
    msgpack=pytest.importorskip('msgpack')
    assert decode_frame(msgpack.packb([{'T':'success','msg':'connected'}]),'opra')[0]['T']=='success'
    path=tmp_path/'options.json'
    path.write_text(json.dumps({'checked_at':datetime.now(timezone.utc).isoformat(),'contracts':[{'symbol':'SPY301218C00600000'},{'symbol':'SPY201218C00600000'}]}))
    assert service.load_option_symbols(path)==('SPY301218C00600000',)


def test_opra_native_msgpack_timestamp_quote_decodes_and_ingests():
    msgpack=pytest.importorskip('msgpack')
    symbol='SPY261218C00600000'
    row=quote(symbol)
    row['t']=msgpack.Timestamp(int(NOW.timestamp()),123456000)
    decoded=decode_frame(msgpack.packb([row],use_bin_type=True),'opra')[0]
    assert decoded['t']==NOW+timedelta(microseconds=123456)
    state=StreamState(feed='opra',quote_symbols=[symbol])
    ready(state)
    received=NOW+timedelta(seconds=1)
    state.ingest(decoded,received)
    health,latest=state.snapshot(received)
    assert health['counts']['invalid']==0
    assert health['counts']['quotes']==1 and health['fresh_counts']['quotes']==1
    assert latest['quotes'][symbol]['data_asof']==decoded['t'].isoformat()
    assert latest['quotes'][symbol]['state']=='fresh'
