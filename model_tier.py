"""Retry a card on a higher model tier after it exhausts its iteration budget.

The ladder is configuration, not code. It is off unless NIGHT_SHIFT_MODEL_LADDER is set, so an
unconfigured worker never changes model and never draws on another quota pool.

The previous attempt is read from the run records the worker already writes. A card only moves up
when its last attempt used the whole iteration budget. Ordinary failures keep the tier, and a
card that stalled without writing moves only after an operator authorized another attempt.
"""
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, List, Mapping, Optional, Set

LADDER_ENV = "NIGHT_SHIFT_MODEL_LADDER"
POOL_CHANGE_ENV = "NIGHT_SHIFT_LADDER_ALLOW_POOL_CHANGE"
SUPPORTED_PROVIDER = "antigravity"

TERMINATION_COMPLETED = "completed"
TERMINATION_ITERATION_CAP = "iteration_cap"
TERMINATION_STALL = "stall_without_write"
TERMINATION_NO_RESPONSE = "no_response"
TERMINATION_ERROR = "error"


class LadderConfigError(ValueError):
    """The ladder configuration cannot be used."""


@dataclass(frozen=True)
class Ladder:
    provider: str
    models: List[str]
    allow_pool_change: bool = False


@dataclass(frozen=True)
class PriorAttempt:
    created_at: str
    outcome: str
    termination: str
    model: Optional[str]
    tier_index: Optional[int]


@dataclass(frozen=True)
class TierDecision:
    provider: str
    model: Optional[str]
    tier_index: Optional[int]
    decision: str
    reason: str
    prior_termination: Optional[str]

    def as_dict(self) -> dict:
        return asdict(self)


def pool_of(model: str) -> str:
    """The quota pool a model draws on, taken from its vendor prefix."""
    return model.split("-", 1)[0]


def load_ladder(env: Mapping[str, str]) -> Optional[Ladder]:
    raw = (env.get(LADDER_ENV) or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise LadderConfigError(f"{LADDER_ENV} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or len(data) != 1:
        raise LadderConfigError(
            f'{LADDER_ENV} must be an object with one provider, for example {{"{SUPPORTED_PROVIDER}": ["model-a", "model-b"]}}'
        )
    ((provider, models),) = data.items()
    if provider != SUPPORTED_PROVIDER:
        raise LadderConfigError(
            f"{LADDER_ENV} supports only {SUPPORTED_PROVIDER}, the one provider whose model list can be checked"
        )
    if not isinstance(models, list) or not models or not all(isinstance(m, str) and m.strip() for m in models):
        raise LadderConfigError(f"{LADDER_ENV} needs a non-empty list of model names, lowest tier first")
    models = [m.strip() for m in models]
    if len(set(models)) != len(models):
        raise LadderConfigError(f"{LADDER_ENV} lists a model more than once")
    allow = (env.get(POOL_CHANGE_ENV) or "").strip().lower() in ("1", "true", "yes")
    return Ladder(provider=provider, models=models, allow_pool_change=allow)


def parse_agy_models(output: str) -> Set[str]:
    """Model ids from `agy models`, which prints `id<TAB>display name` after a status line."""
    ids = set()
    for line in output.splitlines():
        if "\t" not in line:
            continue
        candidate = line.split("\t", 1)[0].strip()
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", candidate):
            ids.add(candidate)
    return ids


def agy_model_catalog(timeout: int = 30) -> Optional[Set[str]]:
    """The models the Antigravity CLI offers right now, or None when it cannot say."""
    try:
        result = subprocess.run(["agy", "models"], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return parse_agy_models(result.stdout) or None


def ladder_problems(ladder: Ladder, catalog: Callable[[], Optional[Set[str]]]) -> List[str]:
    available = catalog()
    if available is None:
        return [f"cannot list the models {ladder.provider} offers, so the ladder cannot be checked"]
    return [f"{model} is not offered by {ladder.provider}" for model in ladder.models if model not in available]


def _model_from_attempts(attempts) -> Optional[str]:
    """The model that served the last provider call, from a name like `Antigravity CLI (model)`."""
    if not isinstance(attempts, list) or not attempts:
        return None
    last = attempts[-1] if isinstance(attempts[-1], dict) else {}
    match = re.search(r"\(([^)]+)\)\s*$", str(last.get("provider", "")))
    return match.group(1) if match else None


def _infer_termination(summary: dict, outcome: str) -> str:
    """Termination for records written before it was recorded, read from their outcome and error text."""
    if outcome == "succeeded":
        return TERMINATION_COMPLETED
    if outcome == "blocked" and summary.get("stall_reason"):
        return TERMINATION_STALL
    if outcome == "failed" and "max iterations" in str(summary.get("error") or "").lower():
        return TERMINATION_ITERATION_CAP
    return TERMINATION_ERROR


def _attempt_from_manifest(manifest: dict) -> PriorAttempt:
    summary = manifest.get("execution_summary") or {}
    outcome = summary.get("terminal_outcome") or "unknown"
    tier = manifest.get("tier_decision") or {}
    return PriorAttempt(
        created_at=str(manifest.get("created_at_iso", "")),
        outcome=outcome,
        termination=summary.get("termination_reason") or _infer_termination(summary, outcome),
        model=_model_from_attempts(summary.get("provider_attempts")) or tier.get("model"),
        tier_index=tier.get("tier_index"),
    )


def load_prior_attempt(record_root, card_id: str) -> Optional[PriorAttempt]:
    """The newest run record for this card, or None when the card has no earlier attempt."""
    root = Path(record_root)
    if not root.is_dir():
        return None
    newest = None
    for path in root.glob("*/manifest.json"):
        try:
            manifest = json.loads(path.read_text())
            stamp = path.stat().st_mtime_ns
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict) or (manifest.get("task") or {}).get("id") != card_id:
            continue
        key = (str(manifest.get("created_at_iso", "")), stamp)
        if newest is None or key > newest[0]:
            newest = (key, manifest)
    return None if newest is None else _attempt_from_manifest(newest[1])


def operator_authorized_retry(history: Optional[list], worker_identity: str) -> bool:
    """True when an operator acted on the card after the worker last released it.

    A stalled card is blocked on the board and only an operator can unblock it, so any later entry
    from someone other than the worker or the system is the authorization to try again.
    """
    entries = [h for h in (history or []) if isinstance(h, dict)]
    releases = [h["at"] for h in entries if h.get("action") == "released" and isinstance(h.get("at"), (int, float))]
    if not releases:
        return False
    last_release = max(releases)
    return any(
        isinstance(h.get("at"), (int, float))
        and h["at"] > last_release
        and not str(h.get("actor", "")).startswith("system:")
        and h.get("actor") != worker_identity
        for h in entries
    )


def _decide(ladder: Ladder, index: int, decision: str, reason: str, prior: Optional[PriorAttempt]) -> TierDecision:
    return TierDecision(
        provider=ladder.provider,
        model=ladder.models[index],
        tier_index=index,
        decision=decision,
        reason=reason,
        prior_termination=prior.termination if prior else None,
    )


def _escalate(ladder: Ladder, index: int, prior: PriorAttempt, why: str) -> TierDecision:
    if index + 1 >= len(ladder.models):
        return _decide(ladder, index, "top_tier", f"{why}, and the ladder has no higher tier", prior)
    current, higher = ladder.models[index], ladder.models[index + 1]
    if pool_of(current) != pool_of(higher) and not ladder.allow_pool_change:
        return _decide(
            ladder, index, "pool_change_denied",
            f"{why}, but {higher} draws on another quota pool and {POOL_CHANGE_ENV} is not set", prior,
        )
    return _decide(ladder, index + 1, "escalated", why, prior)


def choose_tier(ladder: Ladder, prior: Optional[PriorAttempt], *, operator_authorized: bool) -> TierDecision:
    if prior is None or prior.outcome == "succeeded":
        return _decide(ladder, 0, "default", "no earlier failed attempt on record for this card", prior)
    index = ladder.models.index(prior.model) if prior.model in ladder.models else None
    if index is None:
        return _decide(ladder, 0, "held", "the last attempt did not run a ladder model, so its failure says nothing about the ladder", prior)
    if prior.termination == TERMINATION_ITERATION_CAP:
        return _escalate(ladder, index, prior, "the last attempt used its whole iteration budget")
    if prior.termination == TERMINATION_STALL:
        if operator_authorized:
            return _escalate(ladder, index, prior, "an operator authorized another attempt after an exploration stall")
        return _decide(ladder, index, "held", "the card stalled and no operator authorized another attempt, so the tier is unchanged", prior)
    return _decide(ladder, index, "held", f"the last attempt ended with {prior.termination}, which is not an iteration-cap failure", prior)


def disabled(provider: str, model: Optional[str], reason: str) -> TierDecision:
    """A decision that records why a configured ladder was not used."""
    return TierDecision(provider=provider, model=model, tier_index=None, decision="disabled", reason=reason, prior_termination=None)
