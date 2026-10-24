"""Measure what the group extraction prompt produces on real group messages.

Reads the newest group learning jobs out of the live qq memory.db read-only, asks
the real model once per message with EXTRACTION_PROMPT_GROUP, and runs the real
apply_extraction against a throwaway store - so "how many candidates" and "how
many were dropped" are the production gate's own numbers, not a second
implementation of it. No memory row reaches data/, and no key is ever printed:
the only write is the model usage ledger ModelGateway keeps for itself.

    python tools/measure_group_extraction.py --dry        # sample, no model call
    python tools/measure_group_extraction.py --limit 10   # one real call per message
"""
import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'harness')]

from storage import database  # noqa: E402 - needs the path above

DB = ROOT / 'data/qq/memory.db'
# apply_extraction's own confidence gate. Kept here only to label the drops; the
# per-message counts come from running that method, and a mismatch is reported
# instead of being silently absorbed.
THRESHOLD = 0.8
MAX_ITEMS = 8  # apply_extraction reads no more than eight items per result


def recent_jobs(limit):
    """Newest done group learning jobs, returned oldest-first so time reads forwards."""
    if not DB.exists():
        raise SystemExit('没有找到 ' + str(DB))
    db = sqlite3.connect('file:' + str(DB).replace('\\', '/') + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            "SELECT id,scope,source,subject,text FROM learning_jobs "
            "WHERE scope LIKE 'chat:group:%' AND status='done' "
            "AND scope NOT LIKE '%:test:%' AND scope NOT LIKE '%:task:%' "
            'ORDER BY created DESC LIMIT ?', (max(1, int(limit)),)).fetchall()
    finally:
        db.close()
    if not rows:
        raise SystemExit('没有可测量的群学习任务')
    return [dict(row) for row in reversed(rows)]


def sample(jobs):
    return {'mode': 'dry', 'db': str(DB), 'messages': len(jobs),
            'note': '只读样本；--dry 不调模型、不写库',
            'prompt': 'EXTRACTION_PROMPT_GROUP',
            'jobs': [{'index': index, 'scope': job['scope'], 'source': job['source'],
                      'subject': job['subject'], 'text': job['text']}
                     for index, job in enumerate(jobs, 1)]}


def redact(message, key):
    text = str(message)[:200]
    return text.replace(key, '***') if key else text


def item_notes(item):
    """Label one returned item the way the gate will read it."""
    if not isinstance(item, dict):
        return 'invalid', {'reason': 'invalid', 'raw': str(item)[:80]}
    try:
        confidence = float(item.get('confidence', 0))
    except (TypeError, ValueError):
        return 'invalid', {'reason': 'invalid', 'text': str(item.get('text', ''))[:120]}
    entry = {'reason': 'below_threshold' if confidence < THRESHOLD else 'kept',
             'kind': str(item.get('kind', '')), 'evidence': str(item.get('evidence', '')),
             'subject': str(item.get('subject') or ''), 'category': str(item.get('category', '')),
             'confidence': confidence, 'text': str(item.get('text', ''))[:120]}
    return entry['reason'], entry


def measure_one(gateway, store, prompt, job, index):
    """One real extraction for one message: return counts, keep nothing outside the temp store."""
    report = {'index': index, 'scope': job['scope'], 'source': job['source'],
              'subject': job['subject'], 'text': job['text']}
    try:
        result = gateway.json(prompt, job['text'], origin='measure-group-extraction')
    except Exception as exc:  # noqa: BLE001 - one failed call must not stop the sample
        report.update(model_error=redact(type(exc).__name__ + ': ' + str(exc), redact_key(gateway)),
                      returned=0, considered=0, saved=0, below_threshold=0, invalid=0,
                      response_keys=[], memories_array=False, candidates=[], dropped=[])
        return report
    # An empty answer and a shape mismatch both read as "zero candidates"; keep the
    # two apart so a silent parse problem cannot pass for a cautious model.
    response_keys = sorted(result.keys()) if isinstance(result, dict) else []
    items = result.get('memories') if isinstance(result, dict) else None
    array = isinstance(items, list)
    items = items if array else []
    labels = [item_notes(item) for item in items[:MAX_ITEMS]]
    saved = store.apply_extraction(job, result)
    with database(store.path) as db:
        rows = [dict(row) for row in db.execute(
            'SELECT text,kind,evidence,status,subject FROM memories WHERE source=?', (job['source'],))]
    kept = [entry for label, entry in labels if label == 'kept']
    report.update(returned=len(items), considered=len(labels), saved=len(saved),
                  response_keys=response_keys, memories_array=array,
                  below_threshold=sum(1 for label, _ in labels if label == 'below_threshold'),
                  invalid=sum(1 for label, _ in labels if label == 'invalid'),
                  candidates=[{**row, 'text': row['text'][:120]} for row in rows],
                  dropped=[entry for label, entry in labels if label != 'kept'])
    if len(saved) != len(kept) or len(kept) + len(report['dropped']) != len(labels):
        # The gate disagrees with the labels read here: say so rather than round it away.
        report['note'] = '计数不自洽：saved=%d kept=%d labels=%d' % (len(saved), len(kept), len(labels))
    return report


def redact_key(gateway):
    try:
        return getattr(gateway, 'key', '') or ''
    except Exception:  # noqa: BLE001
        return ''


def measure(jobs):
    try:
        from fish_desktop.services import load_user_env
        load_user_env()  # keys live in the User environment, not in this process
    except Exception:  # noqa: BLE001 - a key already in the environment is enough
        pass
    from memory_store import MemoryStore
    from model_gateway import ModelGateway
    try:
        import memory_engine
        memory_engine.enqueue_warm = lambda *args, **kwargs: False  # throwaway store needs no vectors
    except Exception:  # noqa: BLE001
        pass
    gateway = ModelGateway(ROOT, 'qq')
    prompt = MemoryStore.EXTRACTION_PROMPT_GROUP
    with tempfile.TemporaryDirectory(prefix='measure-group-', ignore_cleanup_errors=True) as work:
        store = MemoryStore(Path(work), 'qq')  # the real gate, on a store that is thrown away
        reports = [measure_one(gateway, store, prompt, job, index)
                   for index, job in enumerate(jobs, 1)]
    candidates = [row for report in reports for row in report['candidates']]
    return {'mode': 'live', 'db': str(DB), 'messages': len(reports),
            'prompt': 'EXTRACTION_PROMPT_GROUP', 'threshold': THRESHOLD,
            'model_calls': sum(1 for report in reports if 'model_error' not in report),
            'model_errors': [{'index': report['index'], 'error': report['model_error']}
                             for report in reports if 'model_error' in report],
            'returned': sum(report['returned'] for report in reports),
            'considered': sum(report['considered'] for report in reports),
            'saved_candidates': len(candidates),
            'empty_responses': sum(1 for report in reports if report['returned'] == 0),
            'shape_mismatch': sum(1 for report in reports
                                  if 'model_error' not in report and not report['memories_array']),
            'below_threshold': sum(report['below_threshold'] for report in reports),
            'invalid_items': sum(report['invalid'] for report in reports),
            'all_saved_are_candidates': all(row['status'] == 'candidate' for row in candidates),
            'by_kind': {kind: sum(1 for row in candidates if row['kind'] == kind)
                        for kind in sorted({row['kind'] for row in candidates})},
            'by_evidence': {evidence: sum(1 for row in candidates if row['evidence'] == evidence)
                            for evidence in sorted({row['evidence'] for row in candidates})},
            'note': '候选写进临时库，正式 data/ 的 memories 未被写入；仅模型用量台账照常记账',
            'per_message': reports}


def main(argv=None):
    parser = argparse.ArgumentParser(description='测量群聊专用提取提示在最近群消息上的产出')
    parser.add_argument('--limit', type=int, default=10, help='测量几条最近的群学习任务（默认 10）')
    parser.add_argument('--dry', action='store_true', help='只打印将处理的文本，不调模型')
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding='utf-8')
    jobs = recent_jobs(args.limit)
    summary = sample(jobs) if args.dry else measure(jobs)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
