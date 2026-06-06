import os
import sys
import importlib
import traceback
import inspect
from unittest.mock import MagicMock

# Mock pytest before importing any test module
mock_pytest = MagicMock()
mock_pytest.fixture = lambda *args, **kwargs: (lambda f: f)
mock_pytest.mark = MagicMock()
sys.modules['pytest'] = mock_pytest

# Ensure workspace is in sys.path
workspace_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, workspace_dir)

def main():
    tests_dir = os.path.join(workspace_dir, 'tests')
    test_files = [f[:-3] for f in os.listdir(tests_dir) if f.startswith('test_') and f.endswith('.py')]
    
    passed = 0
    failed = 0
    
    for module_name in sorted(test_files):
        full_module_name = f'tests.{module_name}'
        print(f"Running tests in {full_module_name}...")
        try:
            mod = importlib.import_module(full_module_name)
            for attr in dir(mod):
                if attr.startswith('test_'):
                    func = getattr(mod, attr)
                    if callable(func):
                        print(f"  Running {attr}...", end="")
                        try:
                            # Resolve tmp_path fixture if expected in signature
                            sig = inspect.signature(func)
                            kwargs = {}
                            temp_dir_obj = None
                            if 'tmp_path' in sig.parameters:
                                import tempfile
                                from pathlib import Path
                                temp_dir_obj = tempfile.TemporaryDirectory()
                                kwargs['tmp_path'] = Path(temp_dir_obj.name)
                                
                            if asyncio_is_coroutine_or_similar(func):
                                import asyncio
                                asyncio.run(func(**kwargs))
                            else:
                                func(**kwargs)
                                
                            if temp_dir_obj:
                                temp_dir_obj.cleanup()
                                
                            print(" PASSED")
                            passed += 1
                        except Exception as exc:
                            print(" FAILED")
                            traceback.print_exc()
                            failed += 1
        except Exception as exc:
            print(f"Failed to import/run tests in {full_module_name}: {exc}")
            traceback.print_exc()
            failed += 1
            
    print("\n" + "="*40)
    print(f"Test Summary: {passed} passed, {failed} failed.")
    print("="*40)
    if failed > 0:
        sys.exit(1)

def asyncio_is_coroutine_or_similar(func):
    return inspect.iscoroutinefunction(func)

if __name__ == '__main__':
    main()
