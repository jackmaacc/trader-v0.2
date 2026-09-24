"""Portable, explicit credentials with macOS compatibility and no secret logging."""
import json
import os
import subprocess
import sys


class CredentialError(RuntimeError):
    pass


def paper_credentials(*, environ=None, platform=None, run=None):
    env = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    key, secret = env.get('APCA_API_KEY_ID'), env.get('APCA_API_SECRET_KEY')
    if key or secret:
        if not isinstance(key, str) or not key.strip() or not isinstance(secret, str) or not secret.strip():
            raise CredentialError('incomplete_environment_credentials')
        return key, secret
    if platform != 'darwin':
        raise CredentialError('paper_credentials_not_configured_use_protected_environment')
    invoke = subprocess.run if run is None else run
    try:
        result = invoke(['/usr/bin/security', 'find-generic-password', '-s', 'codex.alpaca-mcp.paper',
                         '-a', 'alpaca-paper', '-w'], capture_output=True, text=True, check=False, timeout=10)
        if result.returncode:
            raise CredentialError('paper_credential_lookup_failed')
        record = json.loads(result.stdout)
        key, secret = record['ALPACA_API_KEY'], record['ALPACA_SECRET_KEY']
        if not all(isinstance(value, str) and value.strip() for value in (key, secret)):
            raise ValueError('invalid value')
        return key, secret
    except CredentialError:
        raise
    except Exception:
        raise CredentialError('paper_credential_record_unavailable_or_invalid') from None
