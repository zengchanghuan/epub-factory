"""
测试 E1：IP 日限流策略
- E1-1 首次请求允许通过
- E1-2 同一 IP 连续 3 次均允许，第 4 次拒绝
- E1-3 不同 IP 独立计数，互不影响
- E1-4 reset_ip 后计数归零，再次可用
- E1-5 get_count 返回当日已用次数
- E1-6 文件大小常量：MAX_FILE_SIZE_BYTES == 20MB
"""
import os
import sys
import tempfile

# 用临时文件隔离测试，不污染生产数据库
_tmp_db = tempfile.mktemp(suffix=".db")
os.environ["FREE_DAILY_LIMIT"] = "3"
os.environ["MAX_FILE_SIZE_MB"] = "20"

sys.path.insert(0, os.path.dirname(__file__))
from app.infra.rate_limiter import RateLimiter, MAX_FILE_SIZE_BYTES, MAX_FILE_SIZE_MB

rl = RateLimiter(db_path=_tmp_db)
IP_A = "1.2.3.4"
IP_B = "5.6.7.8"


def test_e1_1_first_request_allowed():
    allowed, count = rl.check_and_increment(IP_A)
    assert allowed, "首次请求应被允许"
    assert count == 1, f"首次计数应为 1，实际 {count}"


def test_e1_2_limit_after_three():
    rl.reset_ip(IP_A)
    for i in range(3):
        allowed, cnt = rl.check_and_increment(IP_A)
        assert allowed, f"第 {i+1} 次应允许"
    allowed, cnt = rl.check_and_increment(IP_A)
    assert not allowed, "第 4 次应被拒绝"
    assert cnt == 3, f"计数应停在 3，实际 {cnt}"


def test_e1_3_different_ips_independent():
    rl.reset_ip(IP_B)
    for _ in range(3):
        rl.check_and_increment(IP_A)
    allowed, _ = rl.check_and_increment(IP_B)
    assert allowed, "IP_B 未超限，应允许"


def test_e1_4_reset_restores_quota():
    rl.reset_ip(IP_A)
    for _ in range(3):
        rl.check_and_increment(IP_A)
    rl.reset_ip(IP_A)
    allowed, cnt = rl.check_and_increment(IP_A)
    assert allowed, "重置后应恢复配额"
    assert cnt == 1


def test_e1_5_get_count_accurate():
    rl.reset_ip(IP_A)
    assert rl.get_count(IP_A) == 0
    rl.check_and_increment(IP_A)
    rl.check_and_increment(IP_A)
    assert rl.get_count(IP_A) == 2


def test_e1_6_file_size_constant():
    assert MAX_FILE_SIZE_MB == 20
    assert MAX_FILE_SIZE_BYTES == 20 * 1024 * 1024, (
        f"MAX_FILE_SIZE_BYTES 应为 {20*1024*1024}，实际 {MAX_FILE_SIZE_BYTES}"
    )


# ── 运行 ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    tests = [
        test_e1_1_first_request_allowed,
        test_e1_2_limit_after_three,
        test_e1_3_different_ips_independent,
        test_e1_4_reset_restores_quota,
        test_e1_5_get_count_accurate,
        test_e1_6_file_size_constant,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ❌ {t.__name__}: {e}")
            failed += 1

    print(f"\n{'─'*52}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'─'*52}")

    import os as _os
    _os.unlink(_tmp_db)
    sys.exit(1 if failed else 0)
