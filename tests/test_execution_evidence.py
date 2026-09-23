import pandas as pd,pytest
from trader_engine.research.execution_evidence import fixed_trade_cost_audit,QuoteEvidence,evaluate_expected_edge

def trades():return pd.DataFrame({'quantity':[100,50],'entry_price':[10,20],'exit_price':[11,19],'costs':[2.1,1.95],'pnl':[97.9,-51.95]})

def test_fixed_fills_costs_preserve_all_trades():
 summary,rows=fixed_trade_cost_audit(trades(),[0,10,20])
 assert rows.trades.tolist()==[2,2,2]
 assert rows.gross_pnl.tolist()==[50,50,50]
 assert rows.net_pnl.tolist()==pytest.approx([50,45.95,41.90])
 assert summary['break_even_one_way_bps']==pytest.approx(50/4050*10000)

def test_reconciliation_and_short_positions_rejected():
 x=trades();x.loc[0,'pnl']+=1
 with pytest.raises(ValueError,match='reconcile'):fixed_trade_cost_audit(x)
 with pytest.raises(ValueError,match='long'):fixed_trade_cost_audit(trades().assign(direction='short'))

def quote(**kwargs):
 t=pd.Timestamp('2026-09-22 14:00',tz='UTC')
 return QuoteEvidence(**(dict(event_at=t,available_at=t+pd.Timedelta(milliseconds=10),bid=99,ask=101,bid_size_shares=300,ask_size_shares=100)|kwargs))

def test_quote_features_are_descriptive_not_forecast():
 v=quote().at_decision(pd.Timestamp('2026-09-22 14:00:00.100',tz='UTC'))
 assert v['mid']==100 and v['spread_bps']==200 and v['size_weighted_quote_price']==100.5
 assert not v['is_forecast']

@pytest.mark.parametrize('change,decision',[({},'2026-09-22T14:00:00.005Z'),({},'2026-09-22T14:00:02Z'),({'ask':98},'2026-09-22T14:00:00.100Z'),({'bid_size_shares':0},'2026-09-22T14:00:00.100Z')])
def test_bad_or_unavailable_quotes_rejected(change,decision):
 with pytest.raises(ValueError):quote(**change).at_decision(pd.Timestamp(decision))

def edge(**kwargs):
 return evaluate_expected_edge(**(dict(expected_gross_bps=12,uncertainty_bps=2,round_trip_cost_bps=6,inventory_penalty_bps=1,forecast_at='2026-09-22T14:00:00Z',decision_at='2026-09-22T14:00:01Z')|kwargs))

def test_uncalibrated_forecast_never_passes():assert not edge()['eligible_for_research_entry']

def test_positive_edge_requires_costs_uncertainty_and_inventory():
 r=edge(calibration_verified=True);assert r['net_edge_lower_estimate_bps']==3 and r['eligible_for_research_entry'] and not r['approved_for_trading']
 assert not edge(calibration_verified=True,round_trip_cost_bps=10)['eligible_for_research_entry']
 assert not edge(calibration_verified=True,expected_gross_bps=None)['eligible_for_research_entry']
 assert not edge(calibration_verified=True,forecast_at='2026-09-22T14:00:02Z')['eligible_for_research_entry']
