"""Tests for circular import resolution."""
import importlib
import sys


class TestNoCircularImports:
    def test_app_init_does_not_import_app(self):
        """services/app_init.py must not import from app module."""
        # Remove cached modules
        for mod in list(sys.modules):
            if mod.startswith('services.app_init') or mod == 'app':
                del sys.modules[mod]
        import services.app_init as mod
        import inspect
        source = inspect.getsource(mod)
        assert 'from app import' not in source
        assert 'import app' not in source.replace('import app_init', '')

    def test_initialize_app_accepts_watchdog_fn(self):
        """initialize_app should accept start_watchdog_fn kwarg."""
        from services.app_init import initialize_app
        import inspect
        sig = inspect.signature(initialize_app)
        assert 'start_watchdog_fn' in sig.parameters

    def test_circular_import_detection(self):
        """No circular imports between app.py and any services/ module."""
        import os, re
        project = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        # Check no services/*.py imports from app
        services_dir = os.path.join(project, 'services')
        for f in os.listdir(services_dir):
            if not f.endswith('.py'):
                continue
            path = os.path.join(services_dir, f)
            if os.path.isdir(path):
                continue
            with open(path) as fh:
                for i, line in enumerate(fh, 1):
                    if re.match(r'^from app import|^import app\b', line):
                        assert False, f"services/{f}:{i} imports from app: {line.strip()}"
