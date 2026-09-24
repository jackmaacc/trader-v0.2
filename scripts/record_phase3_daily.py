"""Offline frozen daily signal recording, with no broker or prospective credit."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from trader_engine.research.phase3_recording import record_daily_snapshot

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--snapshot',type=Path,required=True)
    p.add_argument('--registry',type=Path,required=True)
    p.add_argument('--database',type=Path,required=True)
    a=p.parse_args()
    if a.database.resolve() in (a.snapshot.resolve(),a.registry.resolve()):
        p.error('Database cannot overwrite source evidence')
    result=record_daily_snapshot(json.loads(a.snapshot.read_text()),json.loads(a.registry.read_text()),a.database)
    print(json.dumps(result,indent=2))

if __name__=='__main__':main()
