"""Benchmark old raw BGRA versus native PNG/Sixel patches."""
import os
import pathlib
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
from driver import Driver

url = sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(os.path.join(HERE, "scroll-fixture.html")).as_uri()
d = Driver(ephemeral=True, width=1200, height=760)
d.navigate(url)
d.wait_load()
d.wait_ready()
time.sleep(0.5)


def measure(label, fn, count=10):
    samples = []
    for _ in range(count):
        start = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - start) * 1000)
    print(f"{label:36s} avg={sum(samples)/len(samples):6.1f}ms min={min(samples):6.1f}ms max={max(samples):6.1f}ms")
    return result


measure("raw screenshot", lambda: d.screenshot(), 5)
measure("native PNG static", lambda: d.native_frame(format="png"), 12)
measure("native Sixel static", lambda: d.native_frame(format="sixel"), 12)

for fmt in ("png", "sixel"):
    d.native_frame(format=fmt, force=True)
    results = []
    for i in range(6):
        d.eval_js(f"scrollTo(0, {i * 20})")
        time.sleep(0.12)
        start = time.perf_counter()
        patches = d.native_frame(format=fmt)
        ms = (time.perf_counter() - start) * 1000
        results.append((ms, sum(len(p[4]) for p in patches)))
    print(fmt, "scroll: avg ms", sum(x[0] for x in results) / len(results), "avg bytes", sum(x[1] for x in results) / len(results))

d.close()
