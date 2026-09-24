"""Local artifact observer. No credentials, network calls or broker capabilities."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import time
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from trader_engine.operations.paper_trial import PaperTrial


def publish(directory, status):
    path = directory/'status.json'; temp = directory/'status.json.tmp'
    fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as file:
        json.dump(status,file,allow_nan=False); file.flush(); os.fsync(file.fileno())
    os.replace(temp,path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts',type=Path,default=ROOT/'artifacts')
    parser.add_argument('--directory',type=Path,default=ROOT/'artifacts/paper_trial')
    parser.add_argument('--once',action='store_true')
    args = parser.parse_args(argv)
    args.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    # Observer lock is independent of all broker execution locks.
    from trader_engine.execution.journal import ExecutionLock
    stop = [False]
    for sig in (signal.SIGINT,signal.SIGTERM):
        signal.signal(sig,lambda *_:stop.__setitem__(0,True))
    os.umask(0o077)
    with ExecutionLock(args.directory/'observer.lock'):
        if shutil.disk_usage(args.directory).free < 1024**3:
            publish(args.directory,dict(checked_at=datetime.now(timezone.utc).isoformat(),status='error',error='disk_headroom_below_1GiB',operational_qualified=False,investment_qualified=False,live_approved=False))
            return 2
        trial = PaperTrial(args.directory)
        try:
            while not stop[0] and not (args.directory/'SHUTDOWN').exists():
                try:
                    if shutil.disk_usage(args.directory).free < 1024**3:
                        raise OSError('disk_guard')
                    publish(args.directory,trial.poll(args.artifacts))
                except Exception:
                    publish(args.directory,dict(checked_at=datetime.now(timezone.utc).isoformat(),status='error',error='observer_storage_or_input_failure',operational_qualified=False,investment_qualified=False,live_approved=False))
                    return 2
                if args.once:return 0
                deadline = time.monotonic()+60
                while time.monotonic()<deadline and not stop[0] and not (args.directory/'SHUTDOWN').exists():time.sleep(1)
        finally:trial.close()
    publish(args.directory,dict(checked_at=datetime.now(timezone.utc).isoformat(),status='shutdown',operational_qualified=False,investment_qualified=False,live_approved=False))
    return 0


if __name__ == '__main__':
    try: raise SystemExit(main())
    except Exception:
        print('{"status":"error","error":"observer_startup_failed"}',flush=True)
        raise SystemExit(2)
