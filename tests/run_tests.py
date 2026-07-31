#!/usr/bin/env python3
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv):
    verbosity = 2 if "-v" in argv else 1

    names, files, expecting_name = [], [], False
    for arg in argv:
        if expecting_name:
            names.append(arg)
            expecting_name = False
        elif arg == "-k":
            expecting_name = True
        elif arg.startswith("-k"):
            names.append(arg[2:])
        elif not arg.startswith("-"):
            files.append(arg)
    if expecting_name:
        print("run_tests.py: -k needs a pattern", file=sys.stderr)
        return 2

    sys.path.insert(0, REPO_ROOT)
    os.chdir(REPO_ROOT)

    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    if names:
        loader.testNamePatterns = [n if "*" in n else f"*{n}*" for n in names]
    for pattern in files or ["test_*.py"]:
        if not pattern.endswith(".py"):
            pattern += ".py"
        suite.addTests(loader.discover(
            start_dir=os.path.join(REPO_ROOT, "tests"),
            pattern=pattern,
            top_level_dir=REPO_ROOT))

    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    if result.testsRun == 0:
        print("run_tests.py: no tests matched — check the -k pattern or filename",
              file=sys.stderr)
        return 2
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
