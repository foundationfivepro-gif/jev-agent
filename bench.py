"""Validate the Python port against the same corpus the Node prototype scored."""
import time, sys
from pathlib import Path
from context_tier import Chunk, select

root = Path("/home/claude/corpus/src")
files = [p for p in root.rglob("*.ts") if not p.name.endswith(".test.ts")]
chunks = [Chunk("goal", "Find where JWT tokens are verified and where token expiry is handled.", kind="request")]
for i, p in enumerate(files):
    chunks.append(Chunk(f"f{i}", p.read_text(encoding="utf-8", errors="replace"), path=str(p.relative_to(root.parent))))

truth = {c.id for c in chunks if "jwt" in c.path}
print(f"{len(files)} files, {sum(c.tokens for c in chunks):,} tokens, {len(truth)} truly JWT\n")

t0 = time.time()
out = select(chunks[0].text, chunks, budget=40_000)
el = time.time() - t0

ranked = sorted(out.scores.items(), key=lambda kv: -kv[1])
by_id = {c.id: c for c in chunks}
print("top 10:")
for cid, s in ranked[:10]:
    print(f"  {'OK' if cid in truth else '  '} {s:.2f}  {by_id[cid].path}")

k = len(truth)
hits = sum(1 for cid, _ in ranked[:k] if cid in truth)
print(f"\nrecall@{k}: {hits}/{k} = {100*hits//k}%")
print(out.summary())
print("canary:", out.canary)
print(f"elapsed: {el:.1f}s")
