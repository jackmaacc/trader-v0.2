from decimal import Decimal
import pytest
from trader_engine.execution.account_risk import AccountRisk
from trader_engine.execution.alpaca_paper import RollingRateLimiter, BrokerError


def risk(tmp_path):
    r=AccountRisk(tmp_path/'risk.json');r.initialize(100000,100000,'2026-09-23');return r


def test_unknown_and_corrupt_fail_closed(tmp_path):
    p=tmp_path/'risk.json';assert not AccountRisk(p).decision().allow_entries
    p.write_text('{}');r=AccountRisk(p);assert r.decision().cancel_entries
    with pytest.raises(ValueError):r.initialize(100000,100000,'2026-09-23')


def test_daily_gap_counts_and_restart_latch(tmp_path):
    r=risk(tmp_path)
    d=r.observe(99400,'2026-09-24',prior_close_equity=100000)
    assert d.cancel_entries and d.drain
    assert AccountRisk(r.path).observe(100500,'2026-09-24').reason=='daily_halt'
    assert r.observe(100500,'2026-09-25',prior_close_equity=100500).allow_entries


def test_deposits_withdrawals_are_not_performance(tmp_path):
    r=risk(tmp_path)
    assert r.observe(120000,'2026-09-23',20000).allow_entries
    assert r.observe(90000,'2026-09-23',-10000).allow_entries
    assert r.state['highwater']=='90000'


def test_drawdown_persists_across_session_restart_and_requires_review(tmp_path):
    r=risk(tmp_path);assert r.observe(97000,'2026-09-23').drain
    r=AccountRisk(r.path)
    assert r.observe(100000,'2026-09-24',prior_close_equity=97000).drain
    with pytest.raises(ValueError):r.review_drawdown_reset('')
    assert r.review_drawdown_reset('review-1').allow_entries


def test_missing_session_baseline_blocks(tmp_path):
    assert not risk(tmp_path).observe(100000,'2026-09-24').allow_entries


def test_entry_limits(tmp_path):
    r=risk(tmp_path)
    assert r.approve_entry('GLD',90,100,99,[]).allow_entries
    assert not r.approve_entry('GLD',101,100,99,[]).allow_entries
    assert not r.approve_entry('GLD',Decimal('1.5'),100,99,[]).allow_entries
    rows=[dict(symbol=s,quantity=100,price=100,stop_price=99,overnight=True) for s in ['SPY','QQQ']]
    assert not r.approve_entry('IWM',1,100,99,rows).allow_entries
    assert not r.approve_entry('TLT',100,100,99,rows,overnight=True).allow_entries
    rows=[dict(symbol=str(s),quantity=100,price=100,stop_price=99) for s in range(5)]
    assert not r.approve_entry('TLT',1,100,99,rows).allow_entries


class Clock:
    def __init__(self):self.t=0.
    def clock(self):return self.t
    def sleep(self,n):self.t+=max(n,1e-9)


def test_seven_monitors_scanner_emergency_dispatch_no_starvation():
    t=Clock();r=RollingRateLimiter(clock=t.clock,sleep=t.sleep)
    # Admission of current request cannot be preempted; all queued background
    # work is preempted. Seven positions plus 68 scan batches remain queued.
    r.acquire(priority='monitor')
    jobs=[r.enqueue('discovery') for _ in range(68)]
    jobs += [r.enqueue('monitor') for _ in range(7)]
    emergency=r.enqueue('emergency')
    assert not r.try_admit(jobs[0])[0]
    ok,delay=r.try_admit(emergency);assert not ok and delay<=1
    t.sleep(delay);assert r.try_admit(emergency)[0]
    assert t.t<=1
    for j in jobs:r.cancel(j)


def test_emergency_quota_and_fifo():
    t=Clock();r=RollingRateLimiter(clock=t.clock,sleep=t.sleep)
    for _ in range(150):r.acquire(priority='monitor')
    normal=r.enqueue('monitor');emergency=r.enqueue('emergency');second=r.enqueue('emergency')
    assert not r.try_admit(second)[0]
    ok,wait=r.try_admit(emergency);t.sleep(wait)
    assert r.try_admit(emergency)[0]
    assert not r.try_admit(normal)[0]
    r.cancel(second);r.cancel(normal)


def test_bounded_queue_and_retry_after():
    t=Clock();r=RollingRateLimiter(clock=t.clock,sleep=t.sleep,max_queue=1)
    j=r.enqueue('entry')
    with pytest.raises(BrokerError):r.enqueue('discovery')
    r.cancel(j);r.throttle(12);r.acquire(priority='emergency');assert t.t==12


def test_missing_baseline_stays_closed_after_restart(tmp_path):
    r=risk(tmp_path);r.observe(100000,'2026-09-24')
    assert not AccountRisk(r.path).decision().allow_entries


def test_emergency_queue_slots_reserved():
    r=RollingRateLimiter(max_queue=16)
    for _ in range(12):r.enqueue('discovery')
    with pytest.raises(BrokerError):r.enqueue('discovery')
    assert r.enqueue('emergency')


def test_cost_aware_risk_cash_and_fixed_universe(tmp_path):
    r=risk(tmp_path)
    assert not r.approve_entry('GLD',100,100,99,[]).allow_entries
    assert r.approve_entry('GLD',100,100,99,[],one_way_impact_bps=0).allow_entries
    assert not r.approve_entry('GLD',1,100,99,[],cash=99).allow_entries
    assert not r.approve_entry('AAPL',1,100,99,[]).allow_entries
    assert r.observe(99500,'2026-09-23').drain
