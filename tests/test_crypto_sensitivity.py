import numpy as np
import pandas as pd
import pytest

from trader_engine.research.crypto_sensitivity import (
    SYMBOLS, prepare, run_batch, assert_reference_parity,
)


def fixture_frames():
    dates = pd.date_range('2025-01-01', '2026-09-23', tz='UTC')
    n = len(dates)
    frames = {}
    for a, symbol in enumerate(SYMBOLS):
        close = 100 * np.exp(.1 * np.sin(np.arange(n) / 12 + a) + .0001 * np.arange(n))
        opening = close * (1 + .002 * np.cos(np.arange(n)))
        # Extreme gaps exercise pending cancellation and a close-based daily halt.
        for d in (390, 425, 490):
            opening[d] *= .2
            close[d] *= .25
        frames[symbol] = pd.DataFrame(dict(open=opening, close=close,
            high=np.maximum(opening, close) * 1.01,
            low=np.minimum(opening, close) * .99, volume=10000.), index=dates)
    return frames


@pytest.mark.parametrize('delay', [0, 1])
@pytest.mark.parametrize('impact', [0., 1., 5., 15., 29.999, 30.])
def test_full_reference_parity_with_signals_and_gaps(delay, impact):
    prepared = prepare(fixture_frames(), '2026-01-01', '2026-09-23')
    assert_reference_parity(prepared, impact, delay)


def test_benchmark_and_batch_cash_accounting():
    prepared = prepare(fixture_frames(), '2026-01-01', '2026-09-23', benchmark=True)
    assert_reference_parity(prepared, 30., 1)
    result = run_batch(prepared, [1., 5., 30.], [0, 1, 0])
    d = result['daily']
    np.testing.assert_allclose(d[:, :, 0] + d[:, :, 2], d[:, :, 1], atol=1e-8)
    np.testing.assert_allclose(d[:, :, 5] - d[:, :, 3] - d[:, :, 4], d[:, :, 6], atol=1e-8)
    assert (d[:, -1, 2] == 0).all()
    assert (d[:, 0, 1] == 100000).all()
    assert (result['fills'].day_index > 0).all()


def test_missing_calendar_and_invalid_delay_rejected():
    frames = fixture_frames()
    frames[SYMBOLS[0]] = frames[SYMBOLS[0]].drop(frames[SYMBOLS[0]].index[-10])
    with pytest.raises(ValueError, match='Missing'):
        prepare(frames, '2026-01-01', '2026-09-23')
    prepared = prepare(fixture_frames(), '2026-01-01', '2026-09-23')
    with pytest.raises(ValueError):
        run_batch(prepared, [5], [2])
