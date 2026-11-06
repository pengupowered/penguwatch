"""Structure metrics for the decomposition review: file sizes, longest functions, rough complexity.

Usage: python -I -X utf8 scripts/code_metrics.py <src/penguwatch dir> [--json]
Complexity = 1 + branches (if/for/while/try/except/with/bool-ops/comprehensions/ternary), a McCabe approximation.
"""
import ast
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
rows, files = [], []
BRANCH = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.With, ast.AsyncWith, ast.IfExp,
          ast.comprehension, ast.Assert)

for path in sorted(root.rglob("*.py")):
    if "__pycache__" in path.parts:
        continue
    src = path.read_text(encoding="utf-8")
    lines = src.count("\n") + 1
    tree = ast.parse(src)
    files.append({"file": str(path.relative_to(root)).replace("\\", "/"), "lines": lines})
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            cx = 1 + sum(isinstance(n, BRANCH) for n in ast.walk(node)) + sum(
                len(n.values) - 1 for n in ast.walk(node) if isinstance(n, ast.BoolOp))
            rows.append({"file": files[-1]["file"], "name": node.name, "line": node.lineno,
                         "length": node.end_lineno - node.lineno + 1, "complexity": cx})

out = {
    "files": sorted(files, key=lambda f: -f["lines"]),
    "python_lines": sum(f["lines"] for f in files),
    "functions": len(rows),
    "longest": sorted(rows, key=lambda r: -r["length"])[:10],
    "most_complex": sorted(rows, key=lambda r: -r["complexity"])[:10],
    "over_60_lines": sum(1 for r in rows if r["length"] > 60),
    "complexity_over_15": sum(1 for r in rows if r["complexity"] > 15),
}
if "--json" in sys.argv:
    print(json.dumps(out, indent=2))
else:
    print(f"python lines: {out['python_lines']}  functions: {out['functions']}  >60 lines: {out['over_60_lines']}  complexity>15: {out['complexity_over_15']}")
    print("largest files:")
    for f in out["files"][:8]:
        print(f"  {f['lines']:>5}  {f['file']}")
    print("longest functions:")
    for r in out["longest"]:
        print(f"  {r['length']:>4} lines  cx={r['complexity']:<3} {r['file']}:{r['line']} {r['name']}")
    print("most complex:")
    for r in out["most_complex"]:
        print(f"  cx={r['complexity']:<3} {r['length']:>4} lines  {r['file']}:{r['line']} {r['name']}")
