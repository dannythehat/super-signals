from pathlib import Path

root = Path(__file__).resolve().parents[2]
resolver = root / "services/api/app/aidy_shadow_resolver.py"
text = resolver.read_text(encoding="utf-8")
old = '_MAX_WINDOW = timedelta(hours=48)\n'
new = '# Bound retry amplification: a continuity gap can reread at most one hour per poll.\n_MAX_WINDOW = timedelta(hours=1)\n'
if text.count(old) != 1:
    raise SystemExit(f"expected one resolver window constant, got {text.count(old)}")
resolver.write_text(text.replace(old, new, 1), encoding="utf-8")

test = root / "services/api/tests/test_aidy_provider_lab_market_truth.py"
t = test.read_text(encoding="utf-8")
needle = '    assert "while cursor < end" not in client\n'
insert = '    assert "while cursor < end" not in client\n    assert "_MAX_WINDOW = timedelta(hours=1)" in resolver\n'
if t.count(needle) != 1:
    raise SystemExit("architecture test anchor missing")
test.write_text(t.replace(needle, insert, 1), encoding="utf-8")
