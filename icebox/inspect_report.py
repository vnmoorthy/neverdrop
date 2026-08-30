"""Inspect a NeverDrop black-box archive.

    python -m icebox.inspect_report reports/report_00021.ndz
"""
import sys
import time

from .outbox import read_ndz


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    d = read_ndz(sys.argv[1])
    print(f"NDBB v1 · mission {d['mission']} · boot {d['boot']} · "
          f"report #{d['report']:03d} · kind {'crash' if d['kind']==1 else 'backfill'}")
    print(f"created {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(d['created']))}"
          f" · delivery {'DELIVERED (ground-verified)' if d['delivered'] else 'PENDING/UNVERIFIED'}")
    print(f"report hash: {'OK' if d['sha_ok'] else 'MISMATCH'}")
    for r in d["records"]:
        m = r["meta"]
        print(f"  tier {r['tier']}: {m.get('n_samples','?')} samples @ "
              f"{m.get('rate',0):.1f} Hz, t {m.get('t0',0):.2f}..{m.get('t1',0):.2f}, "
              f"{len(r['data'])} B, {m.get('total','?')} chunks, "
              f"record crc {'OK' if r['crc_ok'] else 'FAIL'}")


if __name__ == "__main__":
    main()
