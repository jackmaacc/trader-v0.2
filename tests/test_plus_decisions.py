from datetime import datetime, timedelta, timezone
from trader_engine.research.plus_decisions import evaluate, select_universe

NOW = datetime(2026,9,24,15,0,tzinfo=timezone.utc)


def data():
    bars=[]
    for i in range(16):
        bars.append(dict(symbol='SPY',t=(NOW-timedelta(minutes=16-i)).isoformat(),o=100,h=101 if i<15 else 103,l=99,c=100 if i<15 else 102,v=100 if i<15 else 150))
    return ({'complete':True,'provenance':{'feed':'sip'},'records':bars},
            {'complete':True,'provenance':{'feed':'sip'},'records':[dict(symbol='SPY',t=NOW.isoformat(),bp=102,ap=102.01)]})


def test_signal_never_authorizes_order():
    bars,quotes=data();row=evaluate(['SPY'],bars,quotes,NOW)[0]
    assert row['signal'] and row['decision']=='blocked'
    assert row['broker_execution_enabled'] is False
    assert 'order' not in row


def test_incomplete_and_wrong_feed_are_data_blocked():
    bars,quotes=data();bars['complete']=False
    assert evaluate(['SPY'],bars,quotes,NOW)[0]['decision']=='data_blocked'
    bars['complete']=True;bars['provenance']['feed']='iex'
    assert evaluate(['SPY'],bars,quotes,NOW)[0]['signal'] is None


def test_stale_quote_and_unfinished_bar_not_actionable():
    bars,quotes=data();quotes['records'][0]['t']=(NOW-timedelta(seconds=11)).isoformat()
    assert evaluate(['SPY'],bars,quotes,NOW)[0]['decision']=='data_blocked'
    bars['records'][-1]['t']=NOW.isoformat()
    assert evaluate(['SPY'],bars,quotes,NOW)[0]['signal'] is None


def test_dynamic_universe_requires_active_fresh_volume():
    row=dict(symbol='TEST',asset_class='us_equity',feed='sip',retained=False,currently_active=True,state='fresh',data_asof=NOW.isoformat(),volume_asof=NOW.isoformat(),price=50,volume=10000)
    assert 'TEST' in select_universe({'records':[row]},NOW)
    row['volume_asof']=(NOW-timedelta(days=2)).isoformat()
    assert 'TEST' not in select_universe({'records':[row]},NOW)


def test_plus_panel_missing_data(tmp_path):
    from streamlit.testing.v1 import AppTest
    app=AppTest.from_string('from pathlib import Path\nfrom trader_engine.ui.plus_capabilities import render_plus_capabilities\nrender_plus_capabilities(Path('+repr(str(tmp_path))+'))').run()
    assert not app.exception
    assert all(app.dataframe[0].value.status=='not reporting')


def test_stream_merge_requires_sip_and_fresh_newer_timestamp():
    from trader_engine.research.plus_decisions import merge_stream_quotes
    _,quotes=data();quotes['records'][0]['t']=(NOW-timedelta(seconds=2)).isoformat()
    stream=dict(feed='sip',checked_at=NOW.isoformat(),quotes={'SPY':dict(symbol='SPY',source='alpaca_sip',state='fresh',received_at=NOW.isoformat(),data_asof=NOW.isoformat(),bid=102,ask=102.02,bid_size=5,ask_size=5)})
    assert merge_stream_quotes(quotes,stream,NOW)['records'][0]['source_transport']=='sip_websocket'
    stream['feed']='iex'
    assert merge_stream_quotes(quotes,stream,NOW)['records'][0]['ap']==102.01


def test_plus_panel_discloses_stale_and_truncated_assessments(tmp_path):
    import json
    from streamlit.testing.v1 import AppTest
    folder=tmp_path/'plus_research';folder.mkdir()
    (folder/'options.json').write_text(json.dumps({'checked_at':'2020-01-01T00:00:00+00:00','records':[{'symbol':'TEST','quality_status':'passes_quality_screen','quote_asof':'2020-01-01T00:00:00+00:00'}],'sources':[{'underlying':'SPY','assessed_count':100,'input_count':4000,'assessment_truncated':True,'complete':True}]}))
    app=AppTest.from_string('from pathlib import Path\nfrom trader_engine.ui.plus_capabilities import render_plus_capabilities\nrender_plus_capabilities(Path('+repr(str(tmp_path))+'))').run()
    assert not app.exception
    assert app.dataframe[-1].value.quality_status.iloc[0]=='stale_saved_assessment'
    assert any('assessed 100 of 4000' in x.value for x in app.caption)


def test_gapped_bar_window_is_not_a_valid_negative_signal():
    bars,quotes=data();bars['records'][0]['t']=(NOW-timedelta(minutes=18)).isoformat()
    row=evaluate(['SPY'],bars,quotes,NOW)[0]
    assert row['decision']=='data_blocked'
    assert 'invalid_incomplete_or_stale_bar_window' in row['reasons']


def test_retained_stream_quote_does_not_replace_rest():
    from trader_engine.research.plus_decisions import merge_stream_quotes
    _,quotes=data();quotes['records'][0]['t']=(NOW-timedelta(seconds=2)).isoformat()
    stream=dict(feed='sip',checked_at=NOW.isoformat(),quotes={'SPY':dict(symbol='SPY',source='alpaca_sip',state='retained_previous_session',received_at=NOW.isoformat(),data_asof=NOW.isoformat(),bid=102,ask=103,bid_size=1,ask_size=1)})
    assert merge_stream_quotes(quotes,stream,NOW)['records'][0]['ap']==102.01
