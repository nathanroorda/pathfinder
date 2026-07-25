#!/usr/bin/env python3
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv):
    verbosity = 2 if "-v" in argv else 1
    patterns = [a for a in argv if not a.startswith("-")] or ["test_*.py"]

    sys.path.insert(0, REPO_ROOT)
    os.chdir(REPO_ROOT)

    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    for pattern in patterns:
        if not pattern.endswith(".py"):
            pattern += ".py"
        suite.addTests(loader.discover(
            start_dir=os.path.join(REPO_ROOT, "tests"),
            pattern=pattern,
            top_level_dir=REPO_ROOT))

    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
