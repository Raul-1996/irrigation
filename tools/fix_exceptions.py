#!/usr/bin/env python3
"""Automated except Exception replacement tool.

Reads Python files, analyzes try block context, and replaces
generic `except Exception` with specific exception types.
"""
import re
import sys
import os

# Files to process (in priority order)
FILES = [
    'services/zone_control.py',
    'services/mqtt_pub.py',
    'irrigation_scheduler.py',
    'routes/zones_api.py',
    'routes/system_api.py',
    'services/monitors.py',
    'services/sse_hub.py',
    'services/app_init.py',
    'services/watchdog.py',
    'services/observed_state.py',
    'services/telegram_bot.py',
    'services/events.py',
    'services/locks.py',
    'services/logging_setup.py',
    'services/helpers.py',
    'services/auth_service.py',
    'routes/telegram.py',
    'routes/groups_api.py',
    'routes/mqtt_api.py',
    'routes/programs_api.py',
    'routes/settings.py',
    'app.py',
    'utils.py',
    'db/migrations.py',
    'db/zones.py',
    'run.py',
    'basic_auth_proxy.py',
    'ui_agent_demo.py',
    'tools/MQTT_emulator/mqtt_relay_emulator.py',
    'migrations/reencrypt_secrets.py',
]


def get_try_block_context(lines, except_line_idx):
    """Analyze the try block to determine what operations are inside."""
    # Walk backwards from except to find the matching try
    indent = len(lines[except_line_idx]) - len(lines[except_line_idx].lstrip())
    try_content = []
    for i in range(except_line_idx - 1, max(0, except_line_idx - 30), -1):
        line = lines[i]
        stripped = line.strip()
        line_indent = len(line) - len(line.lstrip()) if stripped else 999
        if stripped.startswith('try:') and line_indent == indent:
            break
        if stripped.startswith('except') and line_indent == indent:
            break
        if line_indent > indent:
            try_content.append(stripped)
    
    content = ' '.join(try_content).lower()
    return content


def classify_exception(lines, line_idx, line_text):
    """Classify what specific exception type should replace Exception."""
    stripped = line_text.strip()
    content = get_try_block_context(lines, line_idx)
    
    # Check for import patterns
    if 'import ' in content and not 'json' in content.lower():
        return 'ImportError'
    
    # Check for top-level module imports (very beginning of file)
    if line_idx < 10 and 'import' in content:
        return 'ImportError'
    
    # Queue operations
    if 'put_nowait' in content or 'queue' in content.lower():
        return 'queue.Full'
    
    # JSON operations
    if 'json.loads' in content or 'json.dumps' in content or 'json.load' in content:
        return '(json.JSONDecodeError, KeyError, TypeError, ValueError)'
    
    # MQTT publish/connect/subscribe/reconnect
    if any(x in content for x in ['mqtt', 'publish', 'subscribe', 'cl.connect', 'client.connect', 
                                    'reconnect', 'loop_start', 'loop_stop', 'disconnect']):
        return '(ConnectionError, TimeoutError, OSError)'
    
    # DB operations
    if any(x in content for x in ['db.', 'sqlite', 'cursor', 'execute', 'commit', 'rollback',
                                    'update_zone', 'get_zone', 'add_log', 'get_setting',
                                    'update_group', 'get_group', 'get_program', 'create_',
                                    'delete_', 'finish_zone_run']):
        return '(sqlite3.Error, OSError)'
    
    # File operations
    if any(x in content for x in ['open(', 'read()', 'write(', 'os.path', 'os.makedirs',
                                    'os.remove', 'shutil', 'send_file']):
        return '(IOError, OSError, PermissionError)'
    
    # Integer/float parsing
    if any(x in content for x in ['int(', 'float(', 'strftime', 'strptime', 'datetime.',
                                    'str(', '.strip()', '.split(']):
        return '(ValueError, TypeError, KeyError)'
    
    # Threading
    if any(x in content for x in ['thread', 'threading', 'pool.map', 'threadpoolexecutor']):
        return '(RuntimeError, OSError)'
    
    # HTTP/requests
    if any(x in content for x in ['requests.', 'urlopen', 'http']):
        return '(ConnectionError, TimeoutError, OSError)'
    
    # Dict/attribute access
    if any(x in content for x in ['.get(', 'getattr', '[', 'dict(']):
        return '(KeyError, TypeError, ValueError)'
    
    # Default: keep as Exception but mark intentional if it looks top-level
    return None


def process_file(filepath):
    """Process a single file, replacing except Exception with specific types."""
    with open(filepath, 'r') as f:
        lines = f.readlines()
    
    changes = 0
    needs_imports = set()
    
    for i, line in enumerate(lines):
        # Match 'except Exception' patterns
        m = re.match(r'^(\s*)(except\s+Exception)(\s+as\s+\w+)?(\s*:.*)$', line)
        if not m:
            # Also match bare 'except Exception:'
            m = re.match(r'^(\s*)(except\s+Exception)(\s*:.*)$', line)
            if m:
                indent, exc_part, colon_rest = m.group(1), m.group(2), m.group(3)
                as_part = ''
            else:
                continue
        else:
            indent, exc_part, as_part, colon_rest = m.group(1), m.group(2), m.group(3) or '', m.group(4)
        
        specific = classify_exception(lines, i, line)
        
        if specific is None:
            # Mark as intentional catch-all
            if '# catch-all: intentional' not in line:
                lines[i] = f"{indent}except Exception{as_part}{colon_rest}  # catch-all: intentional\n"
                changes += 1
            continue
        
        # Track needed imports
        if 'sqlite3' in specific:
            needs_imports.add('sqlite3')
        if 'json.' in specific:
            needs_imports.add('json')
        if 'queue.' in specific:
            needs_imports.add('queue')
        
        lines[i] = f"{indent}except {specific}{as_part}{colon_rest}\n"
        changes += 1
    
    # Add missing imports at the top
    if needs_imports:
        # Find first non-comment, non-docstring line
        insert_idx = 0
        for j, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('"""') or stripped.startswith("'''"):
                # Skip docstrings
                if stripped.count('"""') == 1 or stripped.count("'''") == 1:
                    for k in range(j+1, len(lines)):
                        if '"""' in lines[k] or "'''" in lines[k]:
                            insert_idx = k + 1
                            break
                else:
                    insert_idx = j + 1
                continue
            if stripped.startswith('#') or stripped.startswith('"""') or not stripped:
                continue
            if stripped.startswith('import ') or stripped.startswith('from '):
                insert_idx = j + 1
                continue
            break
        
        for imp in sorted(needs_imports):
            # Check if already imported
            already = False
            for line in lines:
                if f'import {imp}' in line:
                    already = True
                    break
            if not already:
                lines.insert(insert_idx, f'import {imp}\n')
                insert_idx += 1
                changes += 1
    
    if changes:
        with open(filepath, 'w') as f:
            f.writelines(lines)
    
    return changes


def main():
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    total = 0
    for f in FILES:
        if os.path.exists(f):
            n = process_file(f)
            if n:
                print(f"  {f}: {n} changes")
                total += n
    print(f"\nTotal: {total} changes across {len(FILES)} files")


if __name__ == '__main__':
    main()
