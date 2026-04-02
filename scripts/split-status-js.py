#!/usr/bin/env python3
"""
Split status.js into logical modules.
Maps line ranges to output files.
"""
import os

SRC = os.path.join(os.path.dirname(__file__), '..', 'static', 'js', 'status.js')
OUT_DIR = os.path.join(os.path.dirname(__file__), '..', 'static', 'js', 'status')

with open(SRC, 'r') as f:
    lines = f.readlines()

total = len(lines)
print(f"Total lines: {total}")

# Find function boundaries
functions = {}
for i, line in enumerate(lines):
    stripped = line.strip()
    # Match function declarations
    for prefix in ['function ', 'async function ', 'var ', 'let ', 'const ']:
        if stripped.startswith(prefix) and '(' in stripped:
            name = stripped.split('(')[0].replace('function ', '').replace('async ', '').replace('var ', '').replace('let ', '').replace('const ', '').strip()
            if name and not name.startswith('//'):
                functions[name] = i + 1  # 1-indexed
                break

print(f"\nFound {len(functions)} top-level declarations")
for name, line in sorted(functions.items(), key=lambda x: x[1]):
    print(f"  L{line}: {name}")
