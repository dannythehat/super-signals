from pathlib import Path

for rel in (
    'services/api/app/paper_critical_execution.py',
    'services/api/app/trading_execution_canonical.py',
):
    path = Path(rel)
    text = path.read_text(encoding='utf-8')
    first = text.find('    def _allocation_pairs(')
    if first < 0:
        raise SystemExit(f'first allocation method missing: {rel}')
    second = text.find('\n    def _allocation_pairs(', first + 1)
    if second < 0:
        raise SystemExit(f'duplicate allocation method missing: {rel}')
    second += 1
    candidates = []
    search_from = second + len('    def _allocation_pairs(')
    for marker in ('\n    @', '\n    def ', '\n    async def '):
        idx = text.find(marker, search_from)
        if idx >= 0:
            candidates.append(idx)
    if not candidates:
        raise SystemExit(f'duplicate allocation method end missing: {rel}')
    end = min(candidates)
    text = text[:second] + text[end + 1:]
    if text.count('    def _allocation_pairs(') != 1:
        raise SystemExit(f'allocation method count not one: {rel}')
    if 'allocate_entry_targets(entries, targets)' not in text:
        raise SystemExit(f'shared helper delegation missing: {rel}')
    path.write_text(text, encoding='utf-8')
