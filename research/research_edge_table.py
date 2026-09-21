
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # корень репозитория (agents/, data/, backtest.py)
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import json, sys
sys.path.insert(0, ".")
import research_ladder as L
import research_engine as E
from multiprocessing import Pool

def main():
    insts = E.load_all()
    rows = []
    with Pool(10) as p:
        for r in p.imap_unordered(L.work, insts, chunksize=4):
            rows.extend(r)
    def cell(sel):
        out = {}
        for h in L.HORIZONS:
            rs = [r for r in rows if r[5] == h and sel(r)]
            if not rs: continue
            n = len(rs); nn = [r[7] for r in rs if r[7]]
            out[h] = {
              "n": n,
              "p1": sum(r[6][0] for r in rs)/n, "p2": sum(r[6][1] for r in rs)/n,
              "stop": sum(r[6][2] for r in rs)/n, "tgt": sum(r[6][3] for r in rs)/n,
              "c_p1": sum(x[0] for x in nn)/len(nn), "c_p2": sum(x[1] for x in nn)/len(nn),
              "c_stop": sum(x[2] for x in nn)/len(nn), "c_tgt": sum(x[3] for x in nn)/len(nn),
            }
        return out
    nc = lambda r: r[1] != "crypto"
    table = {
      "long_0.618": cell(lambda r: nc(r) and r[3]==1 and r[4]==0.618),
      "long_0.786": cell(lambda r: nc(r) and r[3]==1 and r[4]==0.786),
      "long_all":   cell(lambda r: nc(r) and r[3]==1),
      "short_0.618": cell(lambda r: nc(r) and r[3]==-1 and r[4]==0.618),
      "short_0.786": cell(lambda r: nc(r) and r[3]==-1 and r[4]==0.786),
      "short_all":  cell(lambda r: nc(r) and r[3]==-1),
      "crypto_all": cell(lambda r: r[1]=="crypto"),
    }
    json.dump(table, open("edge_table.json","w"), indent=1)
    for k,v in table.items():
        print(k)
        for h,c in v.items():
            print(f"  {h:>3}д n={c['n']:5d}  +1R {c['p1']:.1%}({c['c_p1']:.1%})  +2R {c['p2']:.1%}({c['c_p2']:.1%})  стоп<+1R {c['stop']:.1%}({c['c_stop']:.1%})  точка2<стоп {c['tgt']:.1%}({c['c_tgt']:.1%})")
if __name__ == "__main__":
    main()
