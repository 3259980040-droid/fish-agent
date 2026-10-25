"""Explicit bridge to the vendored MaiBot/A_memorix pure functions Fish reuses.

Fish does not embed the A_memorix host service, its graph/vector runtime or its
configuration system. Only two dependency-free modules are loaded by file path
from the pinned upstream tree:

* ``core/utils/memory_lifecycle_policy.py`` -- deterministic retention/decay
  policy (decide when an unused automatic fact becomes inactive, and when an
  accessed one becomes due again). Fish keeps its own state columns and only
  consumes the decisions.
* ``core/retrieval/score_calibration.py`` -- query-local score calibration and
  weighted reciprocal-rank fusion of retrieval channels.

Upstream code is never modified. A function is only used when its source file is
byte-identical to the pinned SHA-256 below and exports the expected symbols;
otherwise Fish falls back to a local implementation with the same decisions and
reports ``upstream`` in its status so a caller can see what actually ran.

Pinned source: MaiBot @ ff0fe42500f5f7581e9655d1e78379407233207a,
``src/A_memorix`` (AGPL-3.0 by default, see LICENSE / LICENSE-MAIBOT-GPL.md).
Attribution: vendor/sources/MaiBot/MEMORY-INTEGRATION-NOTICE.md.
"""
from __future__ import annotations
from locations import project_path

import hashlib
import importlib.util
import math
import sys
import threading
from pathlib import Path
from statistics import median
from types import ModuleType
from typing import Callable, Dict, Mapping, Optional, Tuple

MAIBOT_COMMIT = 'ff0fe42500f5f7581e9655d1e78379407233207a'
_UPSTREAM_SHA256 = {
    'lifecycle': 'a0e14f14e8dde1bb1beae28d7651d2d50d14ebd8639ec830114c69cce8e53d9e',
    'calibration': '6bf4c50bf1b52491b7de9983fbf1f5d4aefc4c15b462291a2fbbc81308c12f05',
}
_RELATIVE = {
    'lifecycle': 'core/utils/memory_lifecycle_policy.py',
    'calibration': 'core/retrieval/score_calibration.py',
}

_lock = threading.Lock()
_cache: Dict[str, object] = {}


def maibot_root(root: Path) -> Path:
    """Locate the vendored tree: the data root first, then this checkout."""
    root = Path(root)
    checkout = Path(__file__).resolve().parent.parent
    for base in (root, checkout):
        candidate = base / 'vendor' / 'sources' / 'MaiBot' / 'src' / 'A_memorix'
        if candidate.is_dir():
            return candidate
    return project_path(root, 'vendor', 'sources', 'MaiBot', 'src', 'A_memorix')


def _load(root: Path, key: str) -> Optional[ModuleType]:
    """Load one pinned upstream module without touching upstream files."""
    path = maibot_root(root) / _RELATIVE[key]
    try:
        if not path.is_file():
            return None
        if hashlib.sha256(path.read_bytes()).hexdigest() != _UPSTREAM_SHA256[key]:
            return None
        name = '_fish_a_memorix_' + key
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        # dataclasses resolve their module through sys.modules while the class
        # body executes, so the module must be registered before exec_module.
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(name, None)
            raise
        return module
    except Exception:
        return None


def _get(root: Path, key: str):
    cache_key = (str(Path(root).resolve()), key)
    with _lock:
        if cache_key not in _cache:
            _cache[cache_key] = _load(Path(root), key)
        return _cache[cache_key]


# --------------------------------------------------------------------------
# Lifecycle policy (memory_lifecycle_policy.py)
# --------------------------------------------------------------------------

def retention(anchor: float, strength: float, *, now: float, half_life_hours: float) -> float:
    """Current retention after exponential decay since ``anchor``."""
    anchored = max(float(now), float(anchor))
    elapsed = max(0.0, anchored - float(anchor)) / 3600.0
    return min(1.0, max(0.0, float(strength))) * 2.0 ** (-elapsed / float(half_life_hours))


def lifecycle_due_at(anchor: float, strength: float, *, half_life_hours: float, threshold: float) -> Optional[float]:
    """Wall-clock time when retention first falls to ``threshold`` (None if inactive)."""
    strength = min(1.0, max(0.0, float(strength)))
    if strength <= threshold:
        return None
    return float(anchor) + half_life_hours * math.log2(strength / threshold) * 3600.0


def evaluate_due(root: Path, *, strength: float, anchor: float, now: float,
                 half_life_hours: float, threshold: float) -> Dict[str, object]:
    """Evaluate one stored lifecycle row; uses upstream evaluate_lifecycle if pinned."""
    module = _get(root, 'lifecycle')
    if module is not None and hasattr(module, 'evaluate_lifecycle') and hasattr(module, 'RelationLifecycleState'):
        policy = module.RelationLifecyclePolicy(half_life_hours=half_life_hours, freeze_threshold=threshold,
                                                revive_threshold=max(threshold * 1.5, 0.15))
        decision = module.evaluate_lifecycle(module.RelationLifecycleState(strength=strength, anchor_at=anchor),
                                             now=now, policy=policy)
        return {'inactive': bool(decision.is_inactive), 'next_at': decision.next_lifecycle_at,
                'upstream': True, 'reason': decision.inactive_reason or 'decay'}
    due = lifecycle_due_at(anchor, strength, half_life_hours=half_life_hours, threshold=threshold)
    inactive = due is None or retention(anchor, strength, now=now, half_life_hours=half_life_hours) <= threshold
    return {'inactive': inactive, 'next_at': None if inactive else due, 'upstream': False, 'reason': 'decay'}


def touch(root: Path, *, strength: float, anchor: float, now: float, half_life_hours: float,
          threshold: float, event: str = 'ACCESS') -> Dict[str, object]:
    """Reinforce/revive a lifecycle row after a real use; upstream if available."""
    module = _get(root, 'lifecycle')
    if module is not None and all(hasattr(module, name) for name in
                                  ('apply_lifecycle_event', 'RelationLifecycleEvent', 'RelationLifecycleState', 'RelationLifecyclePolicy')):
        policy = module.RelationLifecyclePolicy(half_life_hours=half_life_hours, freeze_threshold=threshold,
                                                revive_threshold=max(threshold * 1.5, 0.15))
        decision = module.apply_lifecycle_event(
            module.RelationLifecycleState(strength=strength, anchor_at=anchor),
            module.RelationLifecycleEvent(str(event).lower()), now=now, policy=policy)
        return {'strength': decision.strength, 'anchor': decision.anchor_at, 'inactive': bool(decision.is_inactive),
                'next_at': decision.next_lifecycle_at, 'upstream': True}
    current = retention(anchor, strength, now=now, half_life_hours=half_life_hours)
    updated = current + 0.5 * (1.0 - current)
    due = lifecycle_due_at(now, updated, half_life_hours=half_life_hours, threshold=threshold)
    return {'strength': updated, 'anchor': max(float(now), float(anchor)), 'inactive': due is None,
            'next_at': due, 'upstream': False}


# --------------------------------------------------------------------------
# Score calibration / fusion (score_calibration.py)
# --------------------------------------------------------------------------

def fuse(root: Path, channels: Mapping[str, Mapping[str, float]], weights: Mapping[str, float], *,
         rrf_k: int = 60) -> Tuple[Dict[str, float], Dict[str, Dict[str, float]], bool]:
    """Weighted RRF fusion of retrieval channels -> (scores, calibrated, upstream)."""
    module = _get(root, 'calibration')
    if module is not None and hasattr(module, 'fuse_score_maps'):
        scores, calibrated = module.fuse_score_maps(channels, weights, method='weighted_rrf', rrf_k=rrf_k)
        return dict(scores), {k: dict(v) for k, v in calibrated.items()}, True
    ranked: Dict[str, Dict[str, float]] = {}
    for channel, values in channels.items():
        finite = {str(k): float(v) for k, v in values.items() if math.isfinite(float(v))}
        ranks = {value: index + 1 for index, value in enumerate(sorted(set(finite.values()), reverse=True))}
        ranked[channel] = {key: (rrf_k + 1.0) / (rrf_k + ranks[value]) for key, value in finite.items()}
    total = {key: 0.0 for values in ranked.values() for key in values}
    for channel, values in ranked.items():
        weight = float(weights.get(channel, 0.0) or 0.0)
        for key, value in values.items():
            total[key] += weight * value
    return total, ranked, False


def percentile(values: Mapping[str, float], method: str = 'rank_percentile') -> Dict[str, float]:
    """Query-local 0..1 calibration used for lexical relevance display."""
    if not values:
        return {}
    distinct = sorted(set(float(v) for v in values.values()), reverse=True)
    if len(distinct) <= 1:
        return {key: 1.0 for key in values}
    rank = {value: index for index, value in enumerate(distinct)}
    return {key: round(1.0 - rank[float(value)] / (len(distinct) - 1), 6) for key, value in values.items()}


def median_or_zero(values) -> float:
    values = [float(v) for v in values]
    return float(median(values)) if values else 0.0


def source_status(root: Path) -> Dict[str, object]:
    """Which pinned upstream functions are actually in use."""
    status: Dict[str, object] = {'commit': MAIBOT_COMMIT, 'root': str(maibot_root(root))}
    for key in ('lifecycle', 'calibration'):
        module = _get(root, key)
        status[key] = {
            'file': _RELATIVE[key],
            'loaded': module is not None,
            'sha256': _UPSTREAM_SHA256[key],
            'license': 'AGPL-3.0 (LICENSE-MAIBOT-GPL.md grant for MaiBot)',
        }
    return status
