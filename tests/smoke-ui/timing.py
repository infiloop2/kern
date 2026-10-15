"""Named elapsed times include setup/cleanup and are emitted even on failure."""
from contextlib import contextmanager
from time import monotonic


@contextmanager
def timed_group(name):
    started = monotonic()
    status = "failed"
    print(f"TIMING start {name}", flush=True)
    try:
        yield
        status = "passed"
    finally:
        print(f"TIMING end {name}: {monotonic() - started:.3f}s ({status})", flush=True)
