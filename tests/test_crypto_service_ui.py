from datetime import datetime, timezone
from trader_engine.ui.crypto_service import service_health


def test_saved_running_flag_does_not_hide_stale_worker():
    now = datetime(2026, 9, 24, 3, tzinfo=timezone.utc)
    assert service_health({'status': 'running', 'checked_at': '2026-09-24T02:00:00Z'}, now)[0] == 'stale'
    assert service_health({'status': 'running', 'checked_at': 'bad'}, now)[0] == 'unknown'
    assert service_health({'status': 'running', 'checked_at': '2026-09-24T03:00:00Z', 'error': 'broker unavailable'}, now)[0] == 'attention'
    assert service_health({'status': 'running', 'checked_at': '2026-09-24T03:00:00Z'}, now)[0] == 'current'


def test_shutdown_and_observation_never_look_active():
    now = datetime(2026, 9, 24, 3, tzinfo=timezone.utc)
    stamp = {'checked_at': '2026-09-24T03:00:00Z'}
    assert service_health({**stamp, 'status': 'shutdown'}, now)[0] == 'attention'
    assert service_health({**stamp, 'mode': 'observe_only'}, now)[0] == 'attention'
