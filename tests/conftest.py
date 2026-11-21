import importlib.machinery
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))


def load_helper():
    path = os.path.join(ROOT, "src", "app-hub-helper")
    loader = importlib.machinery.SourceFileLoader("app_hub_helper", path)
    spec = importlib.util.spec_from_loader("app_hub_helper", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod
