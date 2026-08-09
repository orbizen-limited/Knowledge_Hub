"""Quick local smoke for Phase 1–2 gap helpers (no network / no httpx)."""
from __future__ import annotations

import ast
import re
from pathlib import Path

WORKER = Path(__file__).resolve().parents[1]
APP = WORKER / "app"


def _parse(path: Path) -> None:
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _extract_looks_like_refusal():
    """Eval looks_like_refusal from llm.py without importing httpx."""
    src = (APP / "llm.py").read_text(encoding="utf-8")
    # Grab marker tuple + function body via AST exec of a trimmed module
    tree = ast.parse(src)
    keep = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_REFUSAL_MARKERS":
                    keep.append(node)
        if isinstance(node, ast.FunctionDef) and node.name == "looks_like_refusal":
            keep.append(node)
    mod = ast.Module(body=keep, type_ignores=[])
    ast.fix_missing_locations(mod)
    ns: dict = {"json": __import__("json")}
    exec(compile(mod, "llm_snip.py", "exec"), ns)
    return ns["looks_like_refusal"]


def main() -> None:
    for name in (
        "llm.py",
        "batch_pipeline.py",
        "gemini_batch.py",
        "openai_compat_batch.py",
        "pipeline.py",
        "main.py",
    ):
        _parse(APP / name)

    looks = _extract_looks_like_refusal()
    assert looks("") is True
    assert looks("I cannot help with that request.") is True
    assert looks('{"bottomLine": "ok [1]"}') is False
    assert looks('{"a": 1, "b": 2}') is False

    batch_src = (APP / "batch_pipeline.py").read_text(encoding="utf-8")
    assert 'BATCH_PROVIDERS = frozenset({"gemini", "qwen"})' in batch_src or (
        "gemini" in batch_src and "qwen" in batch_src and "BATCH_PROVIDERS" in batch_src
    )
    assert "openai_compat_batch" in batch_src

    conf = Path(
        r"d:\laravel projects\modern-madiks\doctors-hero\config\knowledge_hub.php"
    ).read_text(encoding="utf-8")
    assert "'qwen'" in conf and "implemented_providers" in conf

    mig = Path(
        r"d:\laravel projects\modern-madiks\doctors-hero\database\migrations"
        r"\2026_08_09_000002_add_fallback_to_kh_llm_settings.php"
    )
    assert mig.exists()
    assert "fallback_enabled" in mig.read_text(encoding="utf-8")

    ui = Path(
        r"d:\laravel projects\modern-madiks\doctorhero-frontend"
        r"\app\(admin)\admin\knowledge-hub\batches\page.jsx"
    )
    assert ui.exists()
    assert "Bulk Batch" in (
        Path(
            r"d:\laravel projects\modern-madiks\doctorhero-frontend"
            r"\app\(admin)\admin\knowledge-hub\page.jsx"
        ).read_text(encoding="utf-8")
    ) or "Bulk Batch Enrich" in Path(
        r"d:\laravel projects\modern-madiks\doctorhero-frontend"
        r"\app\(admin)\admin\knowledge-hub\page.jsx"
    ).read_text(encoding="utf-8")

    print("local-verify: ok")


if __name__ == "__main__":
    main()
