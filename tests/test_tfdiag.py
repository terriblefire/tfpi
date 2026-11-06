"""Run tfdiag's own tests (tfdiag/tests) as part of `python -m unittest discover -s tests`."""


from helpers import ROOT


def load_tests(loader, tests, pattern):
    return loader.discover(str(ROOT / "tfdiag" / "tests"), top_level_dir=str(ROOT))
