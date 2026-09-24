"""Durable account-level risk decisions. No broker mutations or orders.

Caller holds exclusive account ownership. Equity is net marked account equity;
external cashflows are cumulative since initialization, not trading P&L.
"""
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import json
import os


def amount(value):
    x = Decimal(str(value))
    if not x.is_finite():
        raise ValueError('Nonfinite risk input')
    return x


@dataclass(frozen=True)
class RiskDecision:
    allow_entries: bool
    cancel_entries: bool
    drain: bool
    reason: str
    daily_loss: str = '0'
    drawdown: str = '0'


class AccountRisk:
    def __init__(self, path):
        self.path = Path(path)
        self.state = None
        try:
            s = json.loads(self.path.read_text())
            required = {'version','equity','highwater','daily_baseline','cashflow_total','session','daily_halt','drawdown_halt'}
            if not required <= s.keys() or s['version'] != 1:
                raise ValueError('Invalid risk state')
            for key in ('equity','highwater','daily_baseline','cashflow_total'):
                amount(s[key])
            if min(amount(s[k]) for k in ('equity','highwater','daily_baseline')) <= 0:
                raise ValueError('Invalid risk baseline')
            if any(type(s[k]) is not bool for k in ('daily_halt','drawdown_halt')) or not isinstance(s['session'], str) or not s['session']:
                raise ValueError('Invalid risk latch')
            self.state = s
        except (OSError, ValueError, TypeError, KeyError):
            pass

    def _save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + '.tmp')
        try:
            with tmp.open('w') as f:
                json.dump(state, f, sort_keys=True, allow_nan=False)
                f.flush(); os.fsync(f.fileno())
            os.replace(tmp, self.path)
            fd = os.open(self.path.parent, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
        except Exception:
            self.state = None
            raise
        self.state = state

    def initialize(self, equity, prior_close_equity, session, cashflow_total=0):
        if self.path.exists():
            raise ValueError('Existing state requires recovery, not initialization')
        e, b, c = map(amount, (equity, prior_close_equity, cashflow_total))
        if e <= 0 or b <= 0 or not session:
            raise ValueError('Invalid initial baseline')
        self._save(dict(version=1,equity=str(e),highwater=str(max(e,b)),daily_baseline=str(b),cashflow_total=str(c),session=str(session),daily_halt=False,drawdown_halt=False))
        return self.observe(e,session,c)

    def decision(self):
        if self.state is None:
            return RiskDecision(False, True, False, 'unknown_risk_state')
        s=self.state; e=amount(s['equity'])
        daily=max(Decimal(0),1-e/amount(s['daily_baseline']))
        dd=max(Decimal(0),1-e/amount(s['highwater']))
        reason='drawdown_halt' if s['drawdown_halt'] else (s.get('data_halt') or ('daily_halt' if s['daily_halt'] else 'allowed'))
        return RiskDecision(reason=='allowed',reason!='allowed',s['drawdown_halt'] or s['daily_halt'],reason,str(daily),str(dd))

    def observe(self, equity, session, cashflow_total=0, prior_close_equity=None):
        if self.state is None:
            return self.decision()
        s=self.state.copy(); e=amount(equity); c=amount(cashflow_total)
        delta=c-amount(s['cashflow_total'])
        high=amount(s['highwater'])+delta
        baseline=amount(s['daily_baseline'])+delta
        if str(session) != s['session']:
            if str(session) <= s['session'] or prior_close_equity is None:
                s['data_halt']='missing_or_invalid_session_baseline'; self._save(s)
                return self.decision()
            # Prior close must use current cashflow basis, including cashflows
            # since that close. Caller supplies that reconciled baseline.
            baseline=amount(prior_close_equity)
            s['daily_halt']=False
        if e<=0 or baseline<=0 or high<=0:
            s['data_halt']='invalid_equity_or_baseline'; self._save(s)
            return self.decision()
        s.pop('data_halt',None)
        high=max(high,e)
        s.update(equity=str(e),highwater=str(high),daily_baseline=str(baseline),cashflow_total=str(c),session=str(session))
        s['daily_halt']=s['daily_halt'] or e<=baseline*Decimal('.995')
        s['drawdown_halt']=s['drawdown_halt'] or e<=high*Decimal('.97')
        self._save(s)
        return self.decision()

    def review_drawdown_reset(self, review_id):
        if self.state is None or not str(review_id).strip():
            raise ValueError('Known state and explicit review required')
        s=self.state.copy(); s.update(drawdown_halt=False,highwater=s['equity'],review_id=str(review_id))
        self._save(s)
        return self.decision()

    def approve_entry(self, symbol, quantity, price, stop_price, positions, overnight=False,
                      *, one_way_impact_bps=7, commission_bps=0, cash=None, reference_price=None):
        """Price is impact-adjusted entry price; bps are 1/10,000 per side.

        Stop risk includes entry commission and adverse exit impact/commission,
        matching replay. An immediately marketable stop uses the lower of its
        trigger and reference_price (executable bid for quote-based callers).
        Without an explicit reference, invert the modeled entry impact.
        Existing rows can provide exit_cost_per_share in dollars.
        Cash, when supplied, is reconciled unreserved cash, not buying power.
        """
        d=self.decision()
        if not d.allow_entries:return d
        try:
            q,p,stop=map(amount,(quantity,price,stop_price));e=amount(self.state['equity'])
            from trader_engine.research.strategy_spec import ETF_UNIVERSE
            if symbol not in ETF_UNIVERSE:raise ValueError('outside_fixed_etf_universe')
            impact=amount(one_way_impact_bps)/10000;commission=amount(commission_bps)/10000
            if not 0<=impact<1 or not 0<=commission<1:raise ValueError('invalid_cost_rate')
            if cash is not None and q*p*(1+commission)>amount(cash):raise ValueError('insufficient_cash')
            if q<=0 or q!=q.to_integral_value() or not 0<stop<p:
                raise ValueError('Invalid whole-share long')
            reference=p/(1+impact) if reference_price is None else amount(reference_price)
            if reference<=0:raise ValueError('invalid_reference_price')
            executable_stop=min(stop,reference)
            proposed=dict(symbol=symbol,quantity=q,price=p,stop_price=stop,overnight=overnight,
                          exit_cost_per_share=stop-executable_stop*(1-impact)*(1-commission))
            totals=dict(gross=Decimal(0),name=Decimal(0),risk=Decimal(0),overnight=Decimal(0),indexes=Decimal(0))
            for row in [*positions,proposed]:
                rq,rp,rs=map(amount,(row['quantity'],row['price'],row['stop_price']))
                if rq<=0 or rq!=rq.to_integral_value() or not 0<rs<rp:raise ValueError('Invalid existing exposure')
                if row['symbol'] not in ETF_UNIVERSE:raise ValueError('unknown_existing_exposure')
                exit_cost=amount(row.get('exit_cost_per_share',rs*(1-(1-impact)*(1-commission))))
                if exit_cost<0:raise ValueError('invalid_exit_cost')
                n=rq*rp; totals['gross']+=n;totals['risk']+=rq*max(Decimal(0),rp-rs+exit_cost)
                if row is proposed:totals['risk']+=q*p*commission
                if row['symbol']==symbol:totals['name']+=n
                if row.get('overnight',False):totals['overnight']+=n
                if row['symbol'] in ('SPY','QQQ','IWM'):totals['indexes']+=n
            limits={'gross':'.5','name':'.1','risk':'.005','overnight':'.25','indexes':'.2'}
            if q*(p*(1+commission)-executable_stop*(1-impact)*(1-commission))>e*Decimal('.001'):raise ValueError('trade_stop_risk_limit')
            for key,limit in limits.items():
                if totals[key]>e*Decimal(limit):raise ValueError(key+'_limit')
        except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
            return RiskDecision(False,False,False,str(exc))
        return d
