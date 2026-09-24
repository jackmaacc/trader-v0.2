from types import SimpleNamespace
import pytest
from trader_engine.operations.credentials import paper_credentials, CredentialError


def test_linux_environment_never_calls_keychain():
    def forbidden(*a, **k): raise AssertionError('must not invoke')
    assert paper_credentials(environ={'APCA_API_KEY_ID':'test-key','APCA_API_SECRET_KEY':'test-secret'}, platform='linux',run=forbidden)==('test-key','test-secret')


def test_linux_missing_and_partial_environment_fail_safely():
    for env in ({},{'APCA_API_KEY_ID':'secret-looking-value'}):
        with pytest.raises(CredentialError) as error:
            paper_credentials(environ=env,platform='linux')
        assert 'secret-looking-value' not in str(error.value)


def test_mac_existing_keychain_compatibility():
    def fake(*args,**kwargs):
        assert kwargs['timeout']==10
        return SimpleNamespace(returncode=0,stdout='{"ALPACA_API_KEY":"key","ALPACA_SECRET_KEY":"secret"}')
    assert paper_credentials(environ={},platform='darwin',run=fake)==('key','secret')


def test_keychain_errors_cannot_leak_output():
    def fake(*a,**kw): return SimpleNamespace(returncode=0,stdout='sensitive plaintext')
    with pytest.raises(CredentialError) as error:
        paper_credentials(environ={},platform='darwin',run=fake)
    assert 'sensitive plaintext' not in str(error.value)
