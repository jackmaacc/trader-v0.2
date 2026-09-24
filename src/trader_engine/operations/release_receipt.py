"""Read-only build identity. This is never service or trading authorization."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from trader_engine.operations.runtime import normalize_manifest, preflight, service_commands


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def git_identity(root, run=subprocess.run):
    def command(args):
        result=run(['git','-C',str(root),*args],capture_output=True,text=True,timeout=10,check=False)
        if result.returncode:
            raise ValueError('git_identity_unavailable')
        return result.stdout.strip()
    commit=command(['rev-parse','HEAD'])
    if not re.fullmatch(r'[0-9a-f]{40,64}',commit):
        raise ValueError('invalid_git_commit')
    # No remote URLs or arbitrary file names are persisted.
    dirty=bool(command(['status','--porcelain','--untracked-files=normal']))
    return {'commit':commit,'working_tree_clean':not dirty}


def build_receipt(config, *, inspect=preflight, identity=git_identity, now=None):
    manifest=normalize_manifest(config)
    root=Path(manifest['repo_root'])
    source=identity(root)
    checks=inspect(manifest)
    dependency_check=next((c for c in checks['checks'] if c['name']=='dependencies'), {})
    versions=dependency_check.get('detail', {})
    if not isinstance(versions,dict):
        versions={}
    dependency_files={}
    for name in ('pyproject.toml','requirements.txt'):
        path=root/name
        if not path.is_file():
            raise ValueError('dependency_manifest_missing')
        dependency_files[name]=hashlib.sha256(path.read_bytes()).hexdigest()
    failures=[c['name'] for c in checks['checks'] if c['status']=='fail']
    warnings=[c['name'] for c in checks['checks'] if c['status']=='warning']
    command_digest=digest(service_commands(manifest))
    final_source=identity(root)
    if final_source != source:
        failures.append('source_changed_during_checks')
    if not source['working_tree_clean'] or not final_source['working_tree_clean']:
        failures.append('uncommitted_source_changes')
    return dict(version=1,created_at=(now or datetime.now(timezone.utc)).isoformat(),
                purpose='version_identity_for_review_not_activation',source=source,
                manifest_digest=digest(manifest),commands_digest=command_digest,
                dependency_files=dependency_files,installed_dependencies=versions,
                checks_failed=failures,checks_unverified=warnings,
                eligible_for_staging=not failures,execution_authorized=False,live_approved=False,
                limitations=['Command digest covers planned arguments, not installed service configuration',
                             'Identity checks are point-in-time evidence, not a deployment lock or signature',
                             'No broker, credential validity, PC boot, network or cross-host ownership verification',
                             'Installed dependency versions are evidence, not a reproducible cross-platform lockfile',
                             'Rollback of code must never roll back account ownership or pending-order state'])


def verify_receipt(receipt, current):
    if not isinstance(receipt,dict) or receipt.get('version')!=1:
        return {'matches':False,'reasons':['invalid_receipt'],'execution_authorized':False}
    fields=('source','manifest_digest','commands_digest','dependency_files','installed_dependencies')
    reasons=[field+'_changed' for field in fields if receipt.get(field)!=current.get(field)]
    if receipt.get('eligible_for_staging') is not True or current.get('eligible_for_staging') is not True:
        reasons.append('staging_checks_not_passed')
    return dict(matches=not reasons,reasons=reasons,execution_authorized=False,live_approved=False)


def save_receipt(path, receipt):
    path=Path(path)
    # Receipts cannot overwrite configuration, ledger or source files.
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(receipt,stream,indent=2,allow_nan=False);stream.write('\n');stream.flush();os.fsync(stream.fileno())
