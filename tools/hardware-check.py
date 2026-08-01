import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logs import configure_logging
configure_logging()

import camera
from camera import gp2

PASS = "\033[1;32mok  \033[0m"
FAIL = "\033[1;31mFAIL\033[0m"
SKIP = "\033[1;33mskip\033[0m"
NOTE = "\033[1;34mnote\033[0m"

TREE_READ_BUDGET = 1.5


class Checks:
    def __init__(self):
        self.failures = 0
        self.skipped = 0

    def ok(self, claim, condition, detail=""):
        if condition:
            print(f"  {PASS}  {claim}")
            return
        self.failures += 1
        print(f"  {FAIL}  {claim}{f' — {detail}' if detail else ''}")

    def skip(self, claim, why):
        self.skipped += 1
        print(f"  {SKIP}  {claim} — {why}")

    def finding(self, claim, detail):
        print(f"  {NOTE}  {claim} — {detail}")

    def report(self):
        print(f"\n{self.failures} failed, {self.skipped} skipped")
        return 1 if self.failures else 0


def check_magnifier(cam, checks):
    print("\nfocus magnifier")
    state = cam.magnifier()
    if not state["supported"]:
        checks.skip("the body offers focus magnification",
                    "no magnifier_widget quirk for this model")
        return
    checks.ok(f"the body offers focus magnification: "
              f"{', '.join(state['levels'])}", True)

    off = cam._quirks["magnifier_off"]
    costs = []
    try:
        for level in state["levels"]:
            started = time.monotonic()
            result = cam.set_magnifier(level)
            costs.append(time.monotonic() - started)
            checks.ok(f"set {level!r} is reported back as {level!r}",
                      result["value"] == level, f"got {result['value']!r}")

            fresh = cam.magnifier()
            checks.ok(f"{level!r} survives an independent re-read",
                      fresh["value"] == level, f"got {fresh['value']!r}")
    finally:
        cam.set_magnifier(off)

    started = time.monotonic()
    cam.magnifier()
    read = time.monotonic() - started
    best, worst = min(costs), max(costs)
    retried = sum(1 for c in costs if c > read * TREE_READ_BUDGET)
    checks.finding("one whole-tree read costs", f"{read * 1000:.0f}ms")
    checks.finding("a level change holds the bus for",
                   f"{best * 1000:.0f}ms best, {worst * 1000:.0f}ms worst "
                   f"({worst / read:.1f} tree reads); {retried}/{len(costs)} needed "
                   f"a settle retry (liveview gives up at "
                   f"{gp2.PREVIEW_BUS_TIMEOUT:.2f}s)")
    # Ratios, not ms, so the bound holds on any body. Asserted on the *best*
    # change: a settle retry is legitimate and shows up in the worst.
    checks.ok("a settled change costs no more than one tree read",
              best < read * TREE_READ_BUDGET,
              f"{best / read:.1f} tree reads even at best — something "
              f"reintroduced a get_config() into the write path (TODO #49)")


def check_read_paths(cam, checks):
    print("\nread-after-write freshness (TODO #48)")
    name = cam._quirks["magnifier_widget"]
    off = cam._quirks["magnifier_off"]
    claim = "a single-widget read sees a write the tree read sees"
    if not name:
        checks.skip(claim, "no magnifier_widget quirk for this model")
        return

    levels = cam.magnifier()["levels"]
    target = next((l for l in levels if l != off), None)
    if target is None:
        checks.skip(claim, "the body offers no level other than off")
        return

    try:
        cam.set_magnifier(off)
        with cam._bus("hardware-check"):
            cam._drive_action(name, target)
            one = cam._cam.get_single_config(name)
            whole = cam._cam.get_config().get_child_by_name(name)
            single, tree = (gp2._magnifier_level(w.get_value()) for w in (one, whole))
            choices = [gp2._choices(w) for w in (one, whole)]

        checks.ok("a tree read reports the level just written",
                  tree == target,
                  f"wrote {target!r}, tree read {tree!r} — _read_magnifier's "
                  f"whole premise is that this path is fresh")
        if single == tree:
            checks.finding(claim, f"yes, both read {single!r} — this driver does "
                                  f"not need the tree read for freshness")
        else:
            checks.finding(claim, f"no: single={single!r} tree={tree!r} — the "
                                  f"single-widget read is stale, so "
                                  f"_read_magnifier must stay on get_config()")

        checks.ok("the choice list is the same on both paths",
                  choices[0] == choices[1],
                  f"single={choices[0]} tree={choices[1]} — set_magnifier "
                  f"validates off the cheap read and would now reject or admit "
                  f"the wrong levels")
    finally:
        cam.set_magnifier(off)


CHECKS = [check_magnifier, check_read_paths]


def main():
    print("Pathfinder hardware check — no shutter is fired.\n"
          "The service must be stopped first: sudo systemctl stop pathfinder")
    try:
        cam = camera.connect()
    except Exception as exc:
        print(f"\n{FAIL}  could not connect: {exc!r}")
        print("      a running pathfinder service holds the USB claim — stop it first")
        return 2

    print(f"\nconnected: {cam.model}")
    checks = Checks()
    try:
        for check in CHECKS:
            try:
                check(cam, checks)
            except Exception:
                checks.failures += 1
                print(f"  {FAIL}  {check.__name__} raised")
                traceback.print_exc()
    finally:
        camera.disconnect(cam)
    return checks.report()


if __name__ == "__main__":
    sys.exit(main())
