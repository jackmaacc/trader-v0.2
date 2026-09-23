"""Execution evidence for research. No order placement or profitability claims."""
from dataclasses import dataclass
from math import isfinite
import numpy as np
import pandas as pd


def fixed_trade_cost_audit(trades: pd.DataFrame, cost_bps=(0., 7., 14., 28.)) -> tuple[dict, pd.DataFrame]:
    """Long-only recorded fills, identical quantities at every cost assumption.

    This is an attribution counterfactual, not a replay with resized positions.
    Input costs bundle the simulator's execution assumptions, not measured fees.
    """
    required=['quantity','entry_price','exit_price','costs','pnl']
    if not set(required).issubset(trades):raise ValueError('Missing recorded-fill columns')
    levels=np.asarray(cost_bps,dtype=float)
    if levels.ndim!=1 or len(levels)==0 or not np.isfinite(levels).all() or (levels<0).any():raise ValueError('Invalid cost assumptions')
    if 'direction' in trades and not trades.direction.eq('long').all():raise ValueError('This audit supports long fills only')
    x=trades[required].apply(pd.to_numeric,errors='raise')
    if not np.isfinite(x.to_numpy()).all() or (x[['quantity','entry_price','exit_price']]<=0).any().any() or (x.costs<0).any():raise ValueError('Invalid recorded fills')
    gross=x.quantity*(x.exit_price-x.entry_price)
    if not np.allclose(gross-x.costs,x.pnl,atol=1e-6,rtol=1e-9):raise ValueError('Recorded trades do not reconcile')
    turnover=x.quantity*(x.entry_price+x.exit_price)
    gross_total=float(gross.sum());total=float(turnover.sum())
    summary={'trades':len(x),'gross_pnl':gross_total,'recorded_costs':float(x.costs.sum()),'recorded_net_pnl':float(x.pnl.sum()),'traded_notional_both_sides':total,'break_even_one_way_bps':gross_total/total*10000 if total else None,'recorded_effective_one_way_bps':float(x.costs.sum()/total*10000) if total else None,'measured_quote_costs_available':False,'interpretation':'Fixed fills and quantities; no rescaling, alternative trades, or execution guarantees.'}
    rows=[]
    for bps in levels:
        costs=turnover*bps/10000;net=gross-costs;wins=net[net>0].sum();losses=-net[net<0].sum()
        rows.append({'one_way_bps':float(bps),'trades':len(x),'gross_pnl':gross_total,'costs':float(costs.sum()),'net_pnl':float(net.sum()),'net_expectancy_dollars':float(net.mean()) if len(x) else None,'win_rate':float((net>0).mean()) if len(x) else None,'profit_factor':float(wins/losses) if losses else None})
    return summary,pd.DataFrame(rows)


@dataclass(frozen=True)
class QuoteEvidence:
    event_at: pd.Timestamp
    available_at: pd.Timestamp
    bid: float
    ask: float
    bid_size_shares: float
    ask_size_shares: float

    def at_decision(self, decision_at: pd.Timestamp, max_age_ms: float=1000) -> dict:
        """Validate knowledge time and market-event age separately.

        Sizes are shares, not round lots. NBBO sizes are not fill guarantees.
        """
        event=pd.Timestamp(self.event_at);available=pd.Timestamp(self.available_at);decision=pd.Timestamp(decision_at)
        if any(t.tzinfo is None or pd.isna(t) for t in [event,available,decision]):raise ValueError('Timezone-aware, finite timestamps required')
        if not isfinite(max_age_ms) or max_age_ms<0:raise ValueError('Invalid quote age limit')
        if event>available or available>decision:raise ValueError('Quote was not available at decision time')
        age=(decision-event).total_seconds()*1000
        if age>max_age_ms:raise ValueError('Stale quote')
        values=[self.bid,self.ask,self.bid_size_shares,self.ask_size_shares]
        if not all(isfinite(v) and v>0 for v in values) or self.ask<self.bid:raise ValueError('Invalid or crossed quote')
        mid=(self.bid+self.ask)/2
        micro=(self.ask*self.bid_size_shares+self.bid*self.ask_size_shares)/(self.bid_size_shares+self.ask_size_shares)
        return {'mid':mid,'spread_bps':(self.ask-self.bid)/mid*10000,'size_weighted_quote_price':micro,'imbalance':(self.bid_size_shares-self.ask_size_shares)/(self.bid_size_shares+self.ask_size_shares),'event_age_ms':age,'is_forecast':False}


def evaluate_expected_edge(*, expected_gross_bps: float | None, uncertainty_bps: float | None,
                           round_trip_cost_bps: float | None, inventory_penalty_bps: float | None,
                           forecast_at, decision_at, calibration_verified: bool=False) -> dict:
    """A research filter, not broker authorization or a substitute for calibration."""
    result={'eligible_for_research_entry':False,'approved_for_trading':False}
    if calibration_verified is not True:return result|{'reason':'forecast_calibration_unverified'}
    values=[expected_gross_bps,uncertainty_bps,round_trip_cost_bps,inventory_penalty_bps]
    if any(v is None or not isfinite(v) for v in values):return result|{'reason':'missing_or_invalid_estimate'}
    if min(values[1:])<0:raise ValueError('Uncertainty, cost and inventory penalties must be nonnegative')
    forecast=pd.Timestamp(forecast_at);decision=pd.Timestamp(decision_at)
    if any(pd.isna(t) or t.tzinfo is None for t in [forecast,decision]) or forecast>decision:return result|{'reason':'invalid_forecast_timestamp'}
    edge=expected_gross_bps-uncertainty_bps-round_trip_cost_bps-inventory_penalty_bps
    return result|{'eligible_for_research_entry':edge>0,'net_edge_lower_estimate_bps':edge,'reason':'positive_conservative_edge' if edge>0 else 'insufficient_net_edge'}
