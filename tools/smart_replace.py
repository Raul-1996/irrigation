#!/usr/bin/env python3
"""Smart replacement of except Exception with specific types.

Analyzes the try block context to determine the right exception types.
"""
import re
import sys
import os

# Patterns to detect what operations are in a try block
PATTERNS = {
    'db': [
        r'db\.\w+', r'cursor', r'sqlite3', r'\.execute\(', r'\.commit\(',
        r'\.rollback\(', r'get_db\(', r'conn\.', r'database',
        r'db_path', r'SELECT ', r'INSERT ', r'UPDATE ', r'DELETE ',
        r'CREATE TABLE', r'\.fetchone', r'\.fetchall',
    ],
    'mqtt': [
        r'mqtt', r'publish_mqtt', r'\.publish\(', r'paho',
        r'client\.connect', r'client\.subscribe', r'MQTTClient',
        r'normalize_topic', r'mserver', r'mtopic',
    ],
    'json': [
        r'json\.loads', r'json\.dumps', r'json\.load', r'json\.dump',
        r'\.json\(\)', r'request\.get_json',
    ],
    'file': [
        r'open\(', r'\.read\(\)', r'\.write\(', r'os\.path',
        r'os\.remove', r'os\.rename', r'os\.makedirs', r'shutil\.',
        r'pathlib', r'\.readlines\(\)', r'file_path',
    ],
    'parse': [
        r'int\(', r'float\(', r'str\(', r'\.get\(',
        r'\.split\(', r'\.strip\(', r'\.replace\(',
        r'datetime', r'time\.', r'\[.*\]',  # indexing
    ],
    'http': [
        r'requests\.', r'urllib', r'\.get\(.*http', r'\.post\(',
        r'response\.', r'httpx',
    ],
    'import': [
        r'import ', r'from .* import',
    ],
    'thread': [
        r'threading', r'Thread\(', r'\.start\(\)', r'\.join\(',
        r'Lock\(', r'Event\(',
    ],
    'water_monitor': [
        r'water_monitor', r'get_pulses',
    ],
    'events': [
        r'_ev\.publish', r'events\.publish', r'\.publish\(\{',
    ],
    'sse': [
        r'sse', r'SSE', r'event_stream', r'StreamQueue',
    ],
    'logging': [
        r'logging\.', r'logger\.', r'getLogger', r'handler',
        r'RotatingFileHandler', r'StreamHandler',
    ],
    'subprocess': [
        r'subprocess', r'Popen', r'check_output', r'check_call',
        r'os\.system',
    ],
    'telegram': [
        r'telegram', r'bot\.send', r'aiogram', r'send_message',
        r'send_photo', r'reply_text',
    ],
    'apscheduler': [
        r'scheduler', r'add_job', r'remove_job', r'APScheduler',
        r'BackgroundScheduler', r'get_job', r'reschedule_job',
    ],
}

# Exception type mappings
EXCEPTION_MAP = {
    'db': '(sqlite3.Error, OSError)',
    'mqtt': '(ConnectionError, TimeoutError, OSError)',
    'json': '(json.JSONDecodeError, KeyError, TypeError, ValueError)',
    'file': '(IOError, OSError, PermissionError)',
    'parse': '(ValueError, TypeError, KeyError)',
    'http': '(requests.RequestException, ConnectionError, TimeoutError)',
    'import': '(ImportError, AttributeError)',
    'thread': '(RuntimeError, OSError)',
    'water_monitor': '(ValueError, TypeError, AttributeError, OSError)',
    'events': '(ImportError, AttributeError, TypeError)',
    'sse': '(OSError, RuntimeError, ValueError)',
    'logging': '(IOError, OSError, ValueError)',
    'subprocess': '(subprocess.SubprocessError, OSError, ValueError)',
    'telegram': '(ConnectionError, TimeoutError, OSError, ValueError)',
    'apscheduler': '(ValueError, KeyError, RuntimeError)',
}


def find_try_block(lines, except_lineno):
    """Find the try block that corresponds to this except line."""
    # except_lineno is 0-indexed
    except_line = lines[except_lineno]
    except_indent = len(except_line) - len(except_line.lstrip())
    
    # Search backwards for matching try
    for i in range(except_lineno - 1, -1, -1):
        line = lines[i]
        stripped = line.lstrip()
        if stripped.startswith('try:'):
            try_indent = len(line) - len(line.lstrip())
            if try_indent == except_indent:
                return i, except_lineno
    return max(0, except_lineno - 20), except_lineno


def classify_try_block(lines, try_start, except_line):
    """Classify what operations are in the try block."""
    block_text = '\n'.join(lines[try_start:except_line])
    found = set()
    
    for category, patterns in PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, block_text):
                found.add(category)
                break
    
    return found


def get_exception_types(categories):
    """Determine exception types based on categories found."""
    if not categories:
        return '(ValueError, TypeError, RuntimeError)'
    
    all_types = set()
    for cat in categories:
        types_str = EXCEPTION_MAP.get(cat, '')
        # Parse individual types
        for t in re.findall(r'[\w.]+Error|[\w.]+Exception|[\w.]+Warning', types_str):
            all_types.add(t)
    
    if not all_types:
        return '(ValueError, TypeError, RuntimeError)'
    
    # Order: specific first, then generic
    priority = [
        'sqlite3.Error', 'sqlite3.OperationalError',
        'json.JSONDecodeError',
        'requests.RequestException',
        'subprocess.SubprocessError',
        'mqtt.MQTTException',
        'ImportError', 'AttributeError',
        'ConnectionError', 'TimeoutError',
        'IOError', 'OSError', 'PermissionError',
        'ValueError', 'TypeError', 'KeyError',
        'RuntimeError',
    ]
    
    ordered = [t for t in priority if t in all_types]
    # Add any remaining
    for t in sorted(all_types):
        if t not in ordered:
            ordered.append(t)
    
    # Remove IOError if OSError is present (IOError is alias)
    if 'OSError' in ordered and 'IOError' in ordered:
        ordered.remove('IOError')
    
    if len(ordered) == 1:
        return ordered[0]
    return '(' + ', '.join(ordered) + ')'


def is_top_level_handler(lines, except_lineno, filepath):
    """Check if this is a top-level handler (main, before_request, atexit, route handler)."""
    except_line = lines[except_lineno]
    except_indent = len(except_line) - len(except_line.lstrip())
    
    # If indent is 0 or 4 (top-level try or function-level try in main)
    # Look for function context
    for i in range(except_lineno - 1, -1, -1):
        line = lines[i].strip()
        if line.startswith('def ') or line.startswith('class '):
            func_name = line
            # Top-level handlers
            if any(kw in func_name for kw in ['def main', 'before_request', 'atexit', 'teardown', 'errorhandler']):
                return True
            break
    
    # Module-level try/except (indent 0)
    if except_indent == 0:
        return True
    
    return False


def process_file(filepath, dry_run=False):
    """Process a single file."""
    with open(filepath, 'r') as f:
        lines = f.readlines()
    
    changes = []
    
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not re.match(r'except\s+Exception\b', stripped):
            continue
        
        # Check if already has specific comment marking it as truly intentional top-level
        try_start, except_end = find_try_block(lines, i)
        categories = classify_try_block(lines, try_start, i)
        
        is_toplevel = is_top_level_handler(lines, i, filepath)
        
        if is_toplevel and not categories:
            # Truly top-level with no clear pattern - keep as intentional
            # But still try to be specific if we can
            pass
        
        exc_types = get_exception_types(categories)
        
        # Build replacement
        old_except = stripped.rstrip('\n')
        # Parse the existing line format: except Exception as e:  # comment
        m = re.match(r'(except\s+)Exception(\s+as\s+\w+)(\s*:\s*)(.*)', stripped.rstrip('\n'))
        if m:
            new_except = f"{m.group(1)}{exc_types}{m.group(2)}{m.group(3)}{m.group(4)}"
        else:
            # except Exception:  # comment  (no 'as e')
            m2 = re.match(r'(except\s+)Exception(\s*:\s*)(.*)', stripped.rstrip('\n'))
            if m2:
                new_except = f"{m2.group(1)}{exc_types}{m2.group(2)}{m2.group(3)}"
            else:
                continue
        
        # Remove old "# catch-all: intentional" comment
        new_except = re.sub(r'\s*#\s*catch-all:\s*intentional\s*$', '', new_except)
        
        if is_toplevel:
            new_except = new_except.rstrip() + '  # catch-all: intentional'
        
        indent = line[:len(line) - len(line.lstrip())]
        new_line = indent + new_except + '\n'
        
        changes.append((i, line, new_line, categories, is_toplevel))
    
    if dry_run:
        for lineno, old, new, cats, toplevel in changes:
            print(f"  L{lineno+1}: {cats} {'[TOP]' if toplevel else ''}")
            print(f"    OLD: {old.rstrip()}")
            print(f"    NEW: {new.rstrip()}")
        return len(changes)
    
    # Apply changes (reverse order to preserve line numbers)
    for lineno, old, new, cats, toplevel in reversed(changes):
        lines[lineno] = new
    
    with open(filepath, 'w') as f:
        f.writelines(lines)
    
    return len(changes)


def main():
    dry_run = '--dry-run' in sys.argv
    project_root = '/workspace/wb-irrigation'
    
    # Find all project Python files with except Exception
    import subprocess
    result = subprocess.run(
        ['grep', '-rln', 'except Exception', '--include=*.py',
         '--exclude-dir=.venv', '--exclude-dir=tools'],
        capture_output=True, text=True, cwd=project_root
    )
    
    files = [os.path.join(project_root, f.strip()) for f in result.stdout.strip().split('\n') if f.strip()]
    
    total = 0
    for filepath in sorted(files):
        rel = os.path.relpath(filepath, project_root)
        count = process_file(filepath, dry_run)
        if count:
            print(f"{'[DRY]' if dry_run else '[FIX]'} {rel}: {count} replacements")
            total += count
    
    print(f"\nTotal: {total} replacements {'(dry run)' if dry_run else '(applied)'}")


if __name__ == '__main__':
    main()
