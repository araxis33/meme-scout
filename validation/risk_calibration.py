"""Risk score on coins whose truth is known - run after any change to weights.

Targets (23.09.2026): majors 1-2, VIRTUAL 2-3, FLOCK/TIBBIR 3-4, real LAPTOP
4-5, KNNEX (honeypot) 10, LAPTOP copy 9, "liquidity for show" 9.
"""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import checker
sys.stdout.reconfigure(encoding="utf-8")

SET = {
    "AERO": ("0x940181a94a35a4569e4529a3cdfb74e38fd98631", "1-2"),
    "DEGEN": ("0x4ed4e862860bed51a9570b96d89af5e1b0efefed", "1-2"),
    "BRETT": ("0x532f27101965dd16442e59d40670faf5ebb142e4", "1-2"),
    "cbBTC": ("0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf", "1-2"),
    "VIRTUAL": ("0x0b3e328455c4059eeb9e3f84b5543f74e24e7e1b", "2-3"),
    "FLOCK": ("0x5ab3d4c385b400f3abb49e80de2faf6a88a7b691", "3-4"),
    "TIBBIR": ("0xa4a2e2ca3fbfe21aed83471d28b6f65a233c6e00", "3-4"),
    "LAPTOP": ("0xb095274743941e953c746f9c228da9c18bb6ec29", "4-5"),
    "KNNEX": ("0x34f04c3b3bdf1b6850220ed52f4931ea8856f530", "10"),
    "LAPTOPcopy": ("0x24ce3563b69a03a5858eab590c2a5b9da802a203", "9"),
    "CrudeOil": ("0xd2efad15" , "9"),
}

async def main(names):
    for n in names:
        addr, want = SET[n]
        if len(addr) < 42:
            continue
        d = await checker.collect(addr)
        if d.get("error"):
            print(f"{n:11s} want {want:4s} | {d['error'][:60]}"); continue
        head, hard, up, down = checker.assess(d)
        r = d["risk"]
        print(f"{n:11s} want {want:4s} | got {r['score']:2d} (raw {r['raw']:.1f}) {d['tier']['level']:6s} "
              f"| up: {'; '.join(x[:38] for x in (hard + up)[:3])} | down: {'; '.join(x[:30] for x in down[:2])}", flush=True)

asyncio.run(main(sys.argv[1:] or list(SET)))
