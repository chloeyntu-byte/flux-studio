#!/usr/bin/env python3
"""Compare two directory trees, treating numbers as equal within a relative tolerance.

Used by CI to check that regenerated synthetic examples match the committed ones:
NumPy versions may differ in the last printed digit of a float.
usage: compare_numeric_tree.py EXPECTED_DIR ACTUAL_DIR [--rel 1e-9]
"""
import math
import os
import re
import sys

HASH = re.compile(r'("[A-Za-z0-9_]*(?:sha|hash|digest)[A-Za-z0-9_]*"\s*:\s*")[0-9a-fA-F]{16,}(")')
NUM = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")


def main():
    a, b = sys.argv[1], sys.argv[2]
    rel = float(sys.argv[sys.argv.index("--rel") + 1]) if "--rel" in sys.argv else 1e-9
    bad = 0
    files = sorted({os.path.relpath(os.path.join(d, f), a) for d, _, fs in os.walk(a) for f in fs} |
                   {os.path.relpath(os.path.join(d, f), b) for d, _, fs in os.walk(b) for f in fs})
    for rel_path in files:
        pa, pb = os.path.join(a, rel_path), os.path.join(b, rel_path)
        if not (os.path.isfile(pa) and os.path.isfile(pb)):
            print("only in one tree:", rel_path); bad += 1; continue
        try:
            ta, tb = open(pa, encoding="utf-8").read(), open(pb, encoding="utf-8").read()
        except UnicodeDecodeError:
            if open(pa, "rb").read() != open(pb, "rb").read():
                print("binary differs:", rel_path); bad += 1
            continue
        # content hashes of generated files change whenever their last printed digit does; the files
        # themselves are compared numerically, so mask the recorded hashes
        ta, tb = HASH.sub(r"\1<hash>\2", ta), HASH.sub(r"\1<hash>\2", tb)
        sa, sb = NUM.split(ta), NUM.split(tb)
        if sa != sb:
            k = next((i for i, (x, y) in enumerate(zip(sa, sb)) if x != y), min(len(sa), len(sb)))
            ctx = lambda parts: "".join(parts[max(0, k - 2):k + 3])[:160]
            print("text differs: %s\n  expected: %r\n  actual:   %r" % (rel_path, ctx(sa), ctx(sb))); bad += 1; continue
        for x, y in zip(NUM.findall(ta), NUM.findall(tb)):
            if x != y and not math.isclose(float(x), float(y), rel_tol=rel, abs_tol=1e-15):
                print("number differs: %s %s != %s" % (rel_path, x, y)); bad += 1; break
    print("%d file(s) compared, %d difference(s)" % (len(files), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
