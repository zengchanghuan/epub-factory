#!/usr/bin/env python3
"""
回归测试：依次执行 D1–D13 与 C1–C6，汇总通过/失败/跳过。
用于验收「上传、排队、后台、任务中心、通知、下载、失败处理」整条链路。
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 按实施顺序：底座 -> 数据/API -> 任务/阶段 -> Manifest -> 翻译/Reduce -> 打包 -> 状态/通知 -> 增强 -> 回归
D_SUITE = [
    "test_d1_celery_bootstrap.py",
    "test_d2_task_data_models.py",
    "test_d3_api_v2_skeleton.py",
    "test_d4_celery_job_pipeline.py",
    "test_d5_stage_events.py",
    "test_d6_manifest.py",
    "test_d7_translate_chapter.py",
    "test_d8_reduce.py",
    "test_d9_book_reduce.py",
    "test_d10_status_resolver.py",
    "test_d11_notifications.py",
    "test_d12_translation_enhancement.py",
    "test_d13_regression.py",
]
C_SUITE = [
    "test_c1_typography_and_fallback.py",
    "test_c2_pipeline_metrics.py",
    "test_c3_stem_guard.py",
    "test_c4_bilingual.py",
    "test_c5_persistent_store.py",
    "test_c6_glossary_rag.py",
]


def run_one(script: str) -> tuple[bool, str]:
    p = ROOT / script
    if not p.exists():
        return False, f"missing: {script}"
    r = subprocess.run(
        [sys.executable, str(p)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if r.returncode == 0:
        return True, ""
    return False, (r.stderr or r.stdout or f"exit {r.returncode}")[-500:]


def main():
    all_scripts = D_SUITE + C_SUITE
    passed = 0
    failed = []
    for script in all_scripts:
        ok, err = run_one(script)
        if ok:
            passed += 1
            print(f"  ✅ {script}")
        else:
            failed.append((script, err))
            print(f"  ❌ {script}")
            if err:
                print(f"      {err.strip()[:200]}")
    print()
    print("=" * 60)
    print(f"📊 回归结果: {passed}/{len(all_scripts)} 通过")
    if failed:
        print(f"   失败: {[f[0] for f in failed]}")
        sys.exit(1)
    print("=" * 60)


if __name__ == "__main__":
    main()
