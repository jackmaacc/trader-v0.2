"""Attribute recorded long trades at fixed fills; never query or submit orders."""
import argparse,hashlib,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import pandas as pd
from trader_engine.research.execution_evidence import fixed_trade_cost_audit

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--trades',required=True);p.add_argument('--output-dir',required=True);args=p.parse_args()
    source=Path(args.trades);out=Path(args.output_dir)
    if out.exists():raise FileExistsError('Use a new output directory to preserve previous audits')
    summary,cases=fixed_trade_cost_audit(pd.read_csv(source));summary['source_sha256']=hashlib.sha256(source.read_bytes()).hexdigest();summary['created_at']=pd.Timestamp.now(tz='UTC').isoformat()
    out.mkdir(parents=True);cases.to_csv(out/'fixed_trade_costs.csv',index=False);(out/'audit.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary,indent=2))
if __name__=='__main__':main()
