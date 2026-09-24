"""Offline regression tests for provisional archive accounting conventions."""
from pathlib import Path
import sys
import pandas as pd
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from prepare_daily_hypothesis_inputs import provisional_actions, causal_total_return, QQQ_PROVISIONAL_REMOVAL, QQQ_RETAINED

def actions():
    index=pd.DatetimeIndex(['2022-09-19 13:30Z','2022-09-19 13:30Z','2016-03-23 13:30Z'],name='timestamp')
    return pd.DataFrame(dict(symbol=['QQQ','QQQ','IWM'],split_ratio=[1.,1.,1.],cash_dividend=[.51856,.51856,.326642],
       payment_timestamp=pd.to_datetime(['2022-09-23 13:30Z','2022-10-31 13:30Z',None]),
       source_id=[QQQ_PROVISIONAL_REMOVAL,QQQ_RETAINED,'unknown-payment']),index=index)

def test_original_ambiguous_actions_and_unknown_payments_preserved():
    original=actions()
    pd.testing.assert_frame_equal(provisional_actions(original),original)
    assert provisional_actions(original).payment_timestamp.isna().sum()==1

def test_provisional_repair_requires_explicit_flag_and_exact_evidence():
    original=actions();fixed=provisional_actions(original,True)
    assert list(fixed.source_id)==[QQQ_RETAINED,'unknown-payment']
    assert pd.isna(fixed.loc[fixed.source_id=='unknown-payment','payment_timestamp'].iloc[0])
    assert len(original)==3
    changed=original.copy();changed.iloc[0,changed.columns.get_loc('cash_dividend')]=.6
    with pytest.raises(ValueError,match='content changed'):
        provisional_actions(changed,True)

def test_causal_total_return_accounts_ex_date_and_ignores_future_events():
    index=pd.date_range('2022-09-16',periods=4,freq='B',tz='America/New_York')
    frame=pd.DataFrame({'close':[100.,99.48144,100.,101.]},index=index)
    fixed=provisional_actions(actions(),True)
    result=causal_total_return(frame,fixed,'QQQ')
    assert result.iloc[0]==100.
    assert result.iloc[1]==pytest.approx(100.)
    pd.testing.assert_series_equal(result.iloc[:2],causal_total_return(frame.iloc[:2],fixed,'QQQ'))
    with pytest.raises(ValueError,match='Ambiguous'):
        causal_total_return(frame,actions(),'QQQ')

def test_total_return_split_changes_share_count_without_phantom_loss():
    index=pd.date_range('2022-09-16',periods=2,freq='B',tz='America/New_York')
    frame=pd.DataFrame({'close':[100.,50.]},index=index)
    split=pd.DataFrame(dict(symbol=['QQQ'],split_ratio=[2.],cash_dividend=[0.]),index=pd.DatetimeIndex(['2022-09-19 13:30Z']))
    assert causal_total_return(frame,split,'QQQ').iloc[-1]==100.
