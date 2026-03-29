#!/usr/bin/env python3
"""
Diagnostic script: run ONE test file at a time, log all threads before/after.
Shows exactly which threads are created and not cleaned up.
"""
import os
import sys
import threading
import subprocess
import time
import signal

os.environ['TESTING'] = '1'

# Project root
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
TEST_DIR = os.path.dirname(os.path.abspath(__file__))

def get_test_files():
    files = sorted(f for f in os.listdir(TEST_DIR) 
                   if f.startswith('test_') and f.endswith('.py'))
    return files

def run_single_test(filename, timeout=15):
    """Run a single test file with timeout, capture output."""
    filepath = os.path.join(TEST_DIR, filename)
    env = os.environ.copy()
    env['TESTING'] = '1'
    env['PYTHONPATH'] = ROOT
    
    cmd = [
        sys.executable, '-m', 'pytest', filepath,
        '--timeout=8', '-q', '--tb=line', '--no-header',
        '-p', 'no:cacheprovider'
    ]
    
    start = time.time()
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, cwd=ROOT, env=env
        )
        elapsed = time.time() - start
        # Parse last line for summary
        lines = result.stdout.strip().split('\n')
        summary = lines[-1] if lines else '(no output)'
        return {
            'file': filename,
            'status': 'OK',
            'exit_code': result.returncode,
            'elapsed': round(elapsed, 1),
            'summary': summary,
            'stderr_tail': result.stderr.strip().split('\n')[-3:] if result.stderr.strip() else []
        }
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        return {
            'file': filename,
            'status': 'TIMEOUT',
            'exit_code': -1,
            'elapsed': round(elapsed, 1),
            'summary': f'KILLED after {timeout}s',
            'stderr_tail': []
        }

def check_threads_in_process():
    """Import app and check what threads start."""
    print("\n=== THREAD ANALYSIS: What starts on import? ===\n")
    
    before = set(t.name for t in threading.enumerate())
    print(f"Threads BEFORE import: {sorted(before)}")
    
    sys.path.insert(0, ROOT)
    
    # Import conftest (simulating pytest)
    print("\n--- Importing conftest.py ---")
    old_threads = set(t.name for t in threading.enumerate())
    
    # We need to exec conftest in a subprocess to not pollute this process
    env = os.environ.copy()
    env['TESTING'] = '1'
    env['PYTHONPATH'] = ROOT
    
    check_script = '''
import os, sys, threading, time
os.environ["TESTING"] = "1"
os.environ["TEST_DB_PATH"] = "/tmp/debug_test.db"

print("Before any imports:", sorted(t.name for t in threading.enumerate()))

# Simulate what conftest does
sys.path.insert(0, "{root}")

print("\\nImporting app module...")
import app as app_module
print("After app import:", sorted(t.name for t in threading.enumerate()))

print("\\nImporting irrigation_scheduler...")
import irrigation_scheduler
print("After scheduler import:", sorted(t.name for t in threading.enumerate()))

print("\\nCreating test client...")
app_module.app.config["TESTING"] = True
with app_module.app.test_client() as c:
    print("After test_client:", sorted(t.name for t in threading.enumerate()))
    
    # Try a simple request
    print("\\nMaking GET / request...")
    resp = c.get("/")
    print(f"Response: {{resp.status_code}}")
    print("After request:", sorted(t.name for t in threading.enumerate()))

print("\\nFinal threads:", sorted(t.name for t in threading.enumerate()))
for t in threading.enumerate():
    print(f"  {{t.name}}: daemon={{t.daemon}}, alive={{t.is_alive()}}")

# Check if any non-daemon threads would block exit
blockers = [t for t in threading.enumerate() 
            if t.is_alive() and not t.daemon and t.name != "MainThread"]
if blockers:
    print(f"\\n⚠️  NON-DAEMON BLOCKERS: {{[t.name for t in blockers]}}")
else:
    print("\\n✅ No non-daemon blocking threads")

print("\\nExiting in 2s...")
time.sleep(0.5)
'''.format(root=ROOT)
    
    result = subprocess.run(
        [sys.executable, '-c', check_script],
        capture_output=True, text=True, timeout=30,
        cwd=ROOT, env=env
    )
    print(result.stdout)
    if result.stderr:
        # Filter out log noise
        for line in result.stderr.split('\n'):
            if any(kw in line for kw in ['Thread', 'daemon', 'ERROR', 'WARNING', 'block']):
                print(f"  STDERR: {line}")


if __name__ == '__main__':
    print("=" * 70)
    print("WB-IRRIGATION TEST HANG DIAGNOSTICS")
    print("=" * 70)
    
    # Phase 1: Thread analysis
    check_threads_in_process()
    
    # Phase 2: Run each test file individually
    print("\n" + "=" * 70)
    print("INDIVIDUAL TEST FILE RESULTS")
    print("=" * 70 + "\n")
    
    files = get_test_files()
    results = []
    
    ok_count = 0
    timeout_count = 0
    fail_count = 0
    
    for f in files:
        r = run_single_test(f, timeout=20)
        results.append(r)
        
        status_icon = {'OK': '✅', 'TIMEOUT': '🔴'}.get(r['status'], '❓')
        exit_info = f"exit={r['exit_code']}" if r['status'] == 'OK' else ''
        print(f"  {status_icon} {r['file']:45s} {r['elapsed']:5.1f}s  {r['status']:8s} {exit_info}  {r['summary']}")
        
        if r['status'] == 'TIMEOUT':
            timeout_count += 1
        elif r['exit_code'] == 0:
            ok_count += 1
        else:
            fail_count += 1
    
    print(f"\n{'=' * 70}")
    print(f"SUMMARY: {len(files)} files | ✅ {ok_count} clean | ⚠️ {fail_count} failures | 🔴 {timeout_count} timeouts")
    print(f"{'=' * 70}")
