import asyncio,sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import checker
sys.stdout.reconfigure(encoding='utf-8')
toks=sys.argv[1:]
async def main():
    for a in toks:
        name,addr=a.split('=')
        try:
            d=await checker.collect(addr)
            if d.get('error'): print(f"{name:8s} ERROR {d['error']}"); continue
            v,stop,warn,good=checker.assess(d)
            print(f"{name:8s} {v}")
            for s in stop: print("    ⛔",s)
            for s in warn: print("    ⚠️",s)
            print("    tier:",d.get('tier',{}).get('level'),"| rights:",len(d.get('team_rights') or []),"| liq",round(d['liq']),"age_d",round((d['age_h'] or 0)/24),"cex",len((d.get('cg') or {}).get('cex') or []))
        except Exception as e: print(name,"EXC",type(e).__name__,e)
asyncio.run(main())
