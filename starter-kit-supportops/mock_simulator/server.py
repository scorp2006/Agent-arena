import copy
import hashlib
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, Header, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

# Database and configuration
MOCK_DIR = Path(__file__).resolve().parent
DATA_DIR = MOCK_DIR / "data"
DB_PATH = os.getenv("MOCK_DATABASE_PATH", str(MOCK_DIR / "mock_arena.db"))
REVEAL_GROUND_TRUTH = os.getenv("REVEAL_GROUND_TRUTH", "true").lower() in ("1", "true", "yes")

CENT = Decimal("0.01")


# =============================================================================
# GENERATED FROM src/agent_arena/domain/rules.py & models.py
# DO NOT EDIT MANUALLY - Run `python scripts/export_starter_kit.py` to regenerate
# =============================================================================


@dataclass
class EligibilityResult:
    is_eligible: bool
    status: str = "success"
    reason: str | None = None
    policy_ref: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.is_eligible:
            return {"status": self.status}
        res = {"error": self.error or "INELIGIBLE"}
        if self.reason:
            res["reason"] = self.reason
        if self.policy_ref:
            res["policy_ref"] = self.policy_ref
        return res


CENT = Decimal("0.01")


def to_decimal(val: Any) -> Decimal:
    """Converts numeric or string value to 2-decimal Decimal using standard half-up rounding."""
    if isinstance(val, Decimal):
        return val.quantize(CENT, rounding=ROUND_HALF_UP)
    return Decimal(str(val if val is not None else 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def parse_iso(dt_str: str) -> datetime:
    """Parses ISO timestamp string to timezone-aware UTC datetime."""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return datetime(2026, 1, 1, tzinfo=UTC)


def get_authoritative_policy(world_state: dict[str, Any], category: str) -> dict[str, Any] | None:
    """Returns the latest authoritative policy document for a given category.

    If multiple documents exist (e.g. current vs stale/superseded), selects the one
    with the latest updated_at timestamp.
    """
    policies = world_state.get("policies", [])
    candidates = [p for p in policies if isinstance(p, dict) and p.get("category") == category]
    if not candidates:
        # Check general documents as fallback
        docs = world_state.get("documents", [])
        candidates = [d for d in docs if isinstance(d, dict) and d.get("category") == category]
    if not candidates:
        return None

    # Sort by updated_at descending
    candidates.sort(key=lambda p: parse_iso(p.get("updated_at", "1970-01-01T00:00:00Z")), reverse=True)
    return candidates[0]


def check_refund_eligibility(
    world_state: dict[str, Any],
    transaction_id: str,
    amount: float,
    reason: str,
) -> EligibilityResult:
    """Canonical refund eligibility check per SupportOps_PS.md §5.2.

    Checks:
    1. Transaction existence and validity
    2. Not already refunded or refund amount doesn't exceed original charge
    3. No active chargeback or fraud investigation (DOC-1842 §4)
    4. Within refund policy time window (e.g. 30 days) from authoritative policy
    5. Amount within limits
    """
    transactions = {t.get("id"): t for t in world_state.get("transactions", []) if isinstance(t, dict) and "id" in t}
    tx = transactions.get(transaction_id)
    if not tx:
        return EligibilityResult(
            is_eligible=False,
            error="TRANSACTION_NOT_FOUND",
            reason=f"Transaction '{transaction_id}' does not exist in account records.",
        )

    refund_policy = get_authoritative_policy(world_state, "refund")
    policy_doc_id = refund_policy.get("id", "DOC-1001") if refund_policy else "DOC-1001"

    # 1. Check already refunded or amount exceedance (exact Decimal arithmetic)
    already_refunded = tx.get("refund_status") == "refunded"
    total_refunded = to_decimal(tx.get("refunded_amount", 0.0))
    tx_amount = to_decimal(tx.get("amount", 0.0))
    amount_to_refund = to_decimal(amount)

    if already_refunded or (total_refunded >= tx_amount):
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="already_refunded",
            policy_ref=policy_doc_id,
        )

    if (total_refunded + amount_to_refund) > tx_amount:
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="amount_exceeds_transaction",
            policy_ref=policy_doc_id,
        )

    # 2. Check active chargeback / fraud hold (Working example from SupportOps_PS.md §5.2)
    if tx.get("chargeback_status") == "investigation_active" or tx.get("under_fraud_investigation", False):
        hold_policy = get_authoritative_policy(world_state, "dispute_hold")
        hold_doc_id = hold_policy.get("id", "DOC-1842") if hold_policy else "DOC-1842"
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="chargeback_investigation_active",
            policy_ref=hold_doc_id,
        )

    # 3. Check refund window against authoritative policy
    max_days = refund_policy.get("rules", {}).get("refund_window_days", 30) if refund_policy else 30

    current_date_str = world_state.get("current_date", "2026-09-15T00:00:00Z")
    current_dt = parse_iso(current_date_str)
    tx_dt = parse_iso(tx.get("date", current_date_str))
    days_diff = (current_dt - tx_dt).days

    if days_diff > max_days:
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="outside_refund_window",
            policy_ref=policy_doc_id,
        )

    # All checks pass
    return EligibilityResult(
        is_eligible=True,
        status="refunded",
    )


def check_cancellation_eligibility(
    world_state: dict[str, Any],
    customer_id: str,
    subscription_id: str,
) -> EligibilityResult:
    """Canonical cancellation eligibility check per SupportOps_PS.md §5.3.

    Checks:
    1. Subscription existence and ownership
    2. Active subscription status
    3. Contractual lock-in period (requires approved exception to cancel early)
    4. Unresolved billing dispute blocking cancellation
    """
    subscriptions = {s.get("id"): s for s in world_state.get("subscriptions", []) if isinstance(s, dict) and "id" in s}
    sub = subscriptions.get(subscription_id)
    if not sub:
        return EligibilityResult(
            is_eligible=False,
            error="SUBSCRIPTION_NOT_FOUND",
            reason=f"Subscription '{subscription_id}' does not exist.",
        )

    if sub.get("customer_id") != customer_id:
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="subscription_customer_mismatch",
        )

    cancel_policy = get_authoritative_policy(world_state, "cancellation")
    policy_doc_id = cancel_policy.get("id", "DOC-1003") if cancel_policy else "DOC-1003"

    if sub.get("status") == "cancelled":
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="subscription_already_cancelled",
            policy_ref=policy_doc_id,
        )

    # Check unresolved dispute
    if sub.get("has_unresolved_dispute", False):
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="unresolved_billing_dispute",
            policy_ref=policy_doc_id,
        )

    # Check contractual lock-in
    lock_in_until = sub.get("lock_in_until")
    current_date_str = world_state.get("current_date", "2026-09-15T00:00:00Z")
    if lock_in_until:
        lock_in_dt = parse_iso(lock_in_until)
        current_dt = parse_iso(current_date_str)
        if lock_in_dt > current_dt and not sub.get("has_approved_exception", False):
            return EligibilityResult(
                is_eligible=False,
                error="INELIGIBLE",
                reason="lock_in_period_active",
                policy_ref=policy_doc_id,
            )

    return EligibilityResult(
        is_eligible=True,
        status="cancelled",
    )


def check_escalation_validity(
    world_state: dict[str, Any],
    case_id: str,
    team: str,
    reason: str,
    retrieved_evidence_ids: set[str] | list[str] | None = None,
) -> EligibilityResult:
    """Canonical escalation check per SupportOps_PS.md §4.2, §5.3.

    Must include a reason grounded in something retrievable (a policy ref or evidence ID).
    Empty or generic reasons ('customer mad', 'need help') are rejected.
    Keyword mentions ('fraud', 'chargeback') without citing retrievable evidence are strictly rejected.
    """
    if not reason or len(reason.strip()) < 5:
        return EligibilityResult(
            is_eligible=False,
            error="INVALID_ESCALATION",
            reason="reason_not_grounded",
        )

    # Known retrievable IDs in the world state
    retrievable_ids = set()
    for doc in world_state.get("policies", []):
        if isinstance(doc, dict) and doc.get("id"):
            retrievable_ids.add(doc.get("id"))
    for doc in world_state.get("documents", []):
        if isinstance(doc, dict) and doc.get("id"):
            retrievable_ids.add(doc.get("id"))
    for tx in world_state.get("transactions", []):
        if isinstance(tx, dict) and tx.get("id"):
            retrievable_ids.add(tx.get("id"))
    for cs in world_state.get("historical_cases", []):
        if isinstance(cs, dict) and cs.get("case_id"):
            retrievable_ids.add(cs.get("case_id"))
    for sub in world_state.get("subscriptions", []):
        if isinstance(sub, dict) and sub.get("id"):
            retrievable_ids.add(sub.get("id"))
    for cust in world_state.get("customers", []):
        if isinstance(cust, dict) and cust.get("id"):
            retrievable_ids.add(cust.get("id"))

    # If retrieved_evidence_ids is provided (runtime checking in Phase 2/scoring),
    # verify the reason cites an ID that was actually retrieved by the team.
    candidate_ids = set(retrieved_evidence_ids) if retrieved_evidence_ids is not None else retrievable_ids

    # Reason must cite at least one candidate evidence ID
    has_grounded_ref = any(eid in reason for eid in candidate_ids if eid)

    if not has_grounded_ref:
        return EligibilityResult(
            is_eligible=False,
            error="INVALID_ESCALATION",
            reason="reason_not_grounded",
        )

    return EligibilityResult(
        is_eligible=True,
        status="escalated",
    )


def apply_action_to_world(
    world_state: dict[str, Any],
    action_type: str,
    params: dict[str, Any],
    retrieved_evidence_ids: set[str] | list[str] | None = None,
) -> tuple[dict[str, Any], EligibilityResult]:
    """Applies an action to a working copy of world state.

    Returns (mutated_world_state, eligibility_result).
    If ineligible, world state remains completely unmodified (PS §5.1).
    """
    state_copy = copy.deepcopy(world_state)

    if action_type == "issue_refund":
        tx_id = params.get("transaction_id", "")
        amount_dec = to_decimal(params.get("amount", 0.0))
        reason = params.get("reason", "")
        result = check_refund_eligibility(state_copy, tx_id, float(amount_dec), reason)
        if result.is_eligible:
            for tx in state_copy.get("transactions", []):
                if isinstance(tx, dict) and tx.get("id") == tx_id:
                    current_refunded = to_decimal(tx.get("refunded_amount", 0.0))
                    tx_amt = to_decimal(tx.get("amount", 0.0))
                    new_refunded = current_refunded + amount_dec
                    tx["refunded_amount"] = float(new_refunded)
                    tx["refunded_at"] = state_copy.get("current_date", "2026-09-15T00:00:00Z")
                    if new_refunded >= tx_amt:
                        tx["refund_status"] = "refunded"
                    else:
                        tx["refund_status"] = "partially_refunded"
                    break
        return state_copy if result.is_eligible else world_state, result

    elif action_type == "cancel_subscription":
        cust_id = params.get("customer_id", "")
        sub_id = params.get("subscription_id", "")
        result = check_cancellation_eligibility(state_copy, cust_id, sub_id)
        if result.is_eligible:
            for sub in state_copy.get("subscriptions", []):
                if isinstance(sub, dict) and sub.get("id") == sub_id:
                    sub["status"] = "cancelled"
                    sub["cancelled_at"] = state_copy.get("current_date", "2026-09-15T00:00:00Z")
                    sub["auto_renew"] = False
                    break
        return state_copy if result.is_eligible else world_state, result

    elif action_type == "escalate_case":
        case_id = params.get("case_id", "")
        team = params.get("team", "")
        reason = params.get("reason", "")
        result = check_escalation_validity(state_copy, case_id, team, reason, retrieved_evidence_ids)
        if result.is_eligible:
            escalations = state_copy.setdefault("escalations", [])
            escalations.append(
                {
                    "case_id": case_id,
                    "team": team,
                    "reason": reason,
                    "timestamp": state_copy.get("current_date", "2026-09-15T00:00:00Z"),
                }
            )
        return state_copy if result.is_eligible else world_state, result

    elif action_type == "request_verification":
        # Always succeeds (PS §4.2, §5.3)
        cust_id = params.get("customer_id", "")
        vtype = params.get("verification_type", "identity")
        requests = state_copy.setdefault("verification_requests", [])
        requests.append(
            {
                "customer_id": cust_id,
                "verification_type": vtype,
                "timestamp": state_copy.get("current_date", "2026-09-15T00:00:00Z"),
            }
        )
        return state_copy, EligibilityResult(is_eligible=True, status="verification_requested")

    else:
        return world_state, EligibilityResult(is_eligible=True, status="no_action")


# =============================================================================
# END GENERATED DOMAIN SECTION
# =============================================================================


# =============================================================================
# Pydantic Schemas (Participant-Facing Contract Parity)
# =============================================================================


class SearchKnowledgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(..., min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=20)


class GetDocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    document_id: str = Field(..., min_length=1, max_length=100)


class GetCustomerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    customer_id: str = Field(..., min_length=1, max_length=100)


class GetTransactionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    customer_id: str = Field(..., min_length=1, max_length=100)
    start_date: str | None = Field(default=None, max_length=50)
    end_date: str | None = Field(default=None, max_length=50)


class GetSubscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    customer_id: str = Field(..., min_length=1, max_length=100)


class GetPreviousCasesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    customer_id: str = Field(..., min_length=1, max_length=100)
    limit: int = Field(default=5, ge=1, le=50)


class IssueRefundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    transaction_id: str = Field(..., min_length=1, max_length=100)
    amount: float = Field(..., gt=0.0, allow_inf_nan=False)
    reason: str = Field(..., min_length=1, max_length=1000)


class CancelSubscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    customer_id: str = Field(..., min_length=1, max_length=100)
    subscription_id: str = Field(..., min_length=1, max_length=100)


class EscalateCaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    case_id: str = Field(..., min_length=1, max_length=100)
    team: str = Field(..., min_length=1, max_length=100)
    reason: str = Field(..., min_length=1, max_length=1000)


class RequestVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    customer_id: str = Field(..., min_length=1, max_length=100)
    verification_type: str = Field(default="identity", min_length=1, max_length=100)


class CaseClassification(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    category: str = Field(..., min_length=1, max_length=100)
    issue: str = Field(..., min_length=1, max_length=100)
    severity: Literal["low", "medium", "high", "critical"]


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    resolution: Literal["refund", "deny", "escalate", "request_info"]
    escalation_required: bool


class TaskSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    task_id: str = Field(..., min_length=1, max_length=100)
    case_classification: CaseClassification
    decision: Decision
    evidence: list[str] = Field(default_factory=list, max_length=100)
    uncertainties: list[str] = Field(default_factory=list, max_length=100)
    customer_response: str = Field(..., min_length=1, max_length=10000)
    confidence: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)


# Response Schemas
class TaskStartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    customer_message: str
    customer_id: str


class TaskSubmitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    received: bool = True
    task_id: str


class TaskSubmitPracticeResponse(TaskSubmitResponse):
    correct: bool
    expected_resolution: str | None = None
    expected_evidence: list[str] = Field(default_factory=list)
    your_evidence: list[str] = Field(default_factory=list)
    diff_explanation: str


class SubmissionStartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    submission_id: str
    attempt_number: int
    tasks_total: int
    tasks: list[TaskStartResponse] = Field(default_factory=list)


class SubmissionStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["in_progress", "completed", "expired", "interrupted"]
    tasks_completed: int
    tasks_total: int
    time_remaining_seconds: int


class SubmissionFinalizeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    submission_id: str
    status: Literal["completed"]


class BatchTaskSubmitItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    task_id: str
    decision: Decision
    evidence: list[str] = Field(default_factory=list)
    customer_response: str | None = None
    confidence: float | None = None
    case_classification: CaseClassification | dict[str, Any] | None = None
    uncertainties: list[str] = Field(default_factory=list)
    task_started_at: str | None = None
    task_completed_at: str | None = None


class BatchSubmissionSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    answers: list[BatchTaskSubmitItem]


class BatchSubmissionSubmitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    submission_id: str
    status: str
    tasks_submitted: int
    tasks_total: int
    score_pct: float | None = None
    passed: bool | None = None
    inter_task_durations: list[float] = Field(default_factory=list)


class SubmissionAbortResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    submission_id: str
    status: str = "interrupted"
    message: str


# =============================================================================
# SQLite Persistence Layer
# =============================================================================


def merge_entities_by_key(base_list: list[dict], override_list: list[dict], key: str = "id") -> list[dict]:
    res = copy.deepcopy(base_list)
    lookup = {item[key]: i for i, item in enumerate(res) if key in item}
    for item in override_list:
        if key in item and item[key] in lookup:
            res[lookup[item[key]]] = copy.deepcopy(item)
        else:
            res.append(copy.deepcopy(item))
            if key in item:
                lookup[item[key]] = len(res) - 1
    return res


def load_tasks_from_data_dir(data_dir: Path) -> list[dict]:
    tasks_file = data_dir / "tasks.json"
    gt_file = data_dir / "ground_truth.json"
    if not tasks_file.exists() or not gt_file.exists():
        return []

    with open(tasks_file, "r", encoding="utf-8") as f:
        tasks = json.load(f)
    with open(gt_file, "r", encoding="utf-8") as f:
        gts = json.load(f)
    gt_map = {g["task_id"]: g for g in gts}

    def _read_json(fname: str) -> list[dict]:
        p = data_dir / fname
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        return []

    customers = _read_json("customers.json")
    transactions = _read_json("transactions.json")
    subscriptions = _read_json("subscriptions.json")
    policies = _read_json("policies.json")
    previous_cases = _read_json("previous_cases.json")

    compiled_tasks = []
    for t in tasks:
        tid = t["task_id"]
        gt = gt_map.get(tid, {})
        inp = t.get("input_payload", {})
        overrides = t.get("task_overrides", {})

        world = {
            "seed": 1000,
            "current_date": "2026-09-15T00:00:00+00:00",
            "customers": copy.deepcopy(customers),
            "transactions": copy.deepcopy(transactions),
            "subscriptions": copy.deepcopy(subscriptions),
            "policies": copy.deepcopy(policies),
            "documents": copy.deepcopy(policies),
            "historical_cases": copy.deepcopy(previous_cases),
            "previous_cases": copy.deepcopy(previous_cases),
            "verification_requests": [],
            "escalations": [],
            "target_customer_id": inp.get("customer_id", ""),
        }

        if "customers" in overrides:
            world["customers"] = merge_entities_by_key(world["customers"], overrides["customers"], "id")
        if "transactions" in overrides:
            world["transactions"] = merge_entities_by_key(world["transactions"], overrides["transactions"], "id")
        if "subscriptions" in overrides:
            world["subscriptions"] = merge_entities_by_key(world["subscriptions"], overrides["subscriptions"], "id")
        if "policies" in overrides:
            world["policies"] = merge_entities_by_key(world["policies"], overrides["policies"], "id")
            world["documents"] = merge_entities_by_key(world["documents"], overrides["policies"], "id")
        if "previous_cases" in overrides:
            world["previous_cases"] = merge_entities_by_key(
                world["previous_cases"], overrides["previous_cases"], "case_id"
            )
            world["historical_cases"] = merge_entities_by_key(
                world["historical_cases"], overrides["previous_cases"], "case_id"
            )

        compiled_tasks.append(
            {
                "task_id": tid,
                "dataset": t.get("dataset", "dev"),
                "input_payload": inp,
                "world_state_seed": world,
                "ground_truth": gt,
            }
        )

    return compiled_tasks


def init_db(conn: sqlite3.Connection | None = None) -> None:
    """Initializes SQLite tables and loads tasks from DATA_DIR."""
    close_when_done = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_when_done = True
    try:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mock_tasks (
                    task_id TEXT PRIMARY KEY,
                    dataset TEXT DEFAULT 'dev',
                    input_payload TEXT NOT NULL,
                    world_state_seed TEXT NOT NULL,
                    ground_truth_privileged TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mock_task_assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    task_id TEXT NOT NULL REFERENCES mock_tasks(task_id),
                    submission_id TEXT,
                    assigned_at TIMESTAMP NOT NULL,
                    world_runtime_state TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mock_tool_call_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    submission_id TEXT,
                    tool_name TEXT NOT NULL,
                    request_payload TEXT NOT NULL,
                    response_payload TEXT NOT NULL,
                    was_enforcement_rejection INTEGER NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mock_submissions (
                    submission_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    started_at TIMESTAMP NOT NULL,
                    completed_at TIMESTAMP,
                    per_task_results TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mock_task_evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    customer_id TEXT,
                    customer_message TEXT,
                    correct INTEGER NOT NULL,
                    actual_resolution TEXT,
                    expected_resolution TEXT,
                    actual_escalation INTEGER,
                    expected_escalation INTEGER,
                    actual_evidence TEXT,
                    expected_evidence TEXT,
                    missing_evidence TEXT,
                    diff_explanation TEXT,
                    tool_calls TEXT,
                    submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM mock_tasks")
            count = cursor.fetchone()[0]
            if count == 0:
                tasks = []
                if DATA_DIR.exists():
                    tasks = load_tasks_from_data_dir(DATA_DIR)

                for t in tasks:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO mock_tasks (task_id, dataset, input_payload, world_state_seed, ground_truth_privileged)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            t["task_id"],
                            t.get("dataset", "dev"),
                            json.dumps(t["input_payload"]),
                            json.dumps(t["world_state_seed"]),
                            json.dumps(t["ground_truth"]),
                        ),
                    )
    finally:
        if close_when_done:
            conn.close()


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='mock_tasks'")
    if not cursor.fetchone():
        init_db(conn)
    return conn


def get_session_id(authorization: str | None) -> str:
    """Derives a secure session identifier from the Authorization header using SHA-256."""
    if not authorization:
        return "dev-default-session"
    token = authorization.replace("Bearer ", "").strip()
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def get_active_assignment(
    conn: sqlite3.Connection, session_id: str, task_id: str | None = None
) -> tuple[int, str, dict[str, Any], str | None]:
    """Retrieves the active task assignment for the session, matching task_id if provided."""
    cursor = conn.cursor()
    if task_id:
        cursor.execute(
            """
            SELECT id, task_id, world_runtime_state, submission_id
            FROM mock_task_assignments
            WHERE session_id = ? AND task_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (session_id, task_id),
        )
        row = cursor.fetchone()
        if row:
            return row["id"], row["task_id"], json.loads(row["world_runtime_state"]), row["submission_id"]

    cursor.execute(
        """
        SELECT id, task_id, world_runtime_state, submission_id
        FROM mock_task_assignments
        WHERE session_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (session_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "NO_ACTIVE_TASK",
                "message": "No active task assignment found for team. Start a task before using tools.",
            },
        )
    return row["id"], row["task_id"], json.loads(row["world_runtime_state"]), row["submission_id"]


def update_world_state(conn: sqlite3.Connection, assignment_id: int, new_state: dict[str, Any]) -> None:
    with conn:
        conn.execute(
            "UPDATE mock_task_assignments SET world_runtime_state = ? WHERE id = ?",
            (json.dumps(new_state), assignment_id),
        )


def log_tool_call(
    conn: sqlite3.Connection,
    session_id: str,
    task_id: str,
    submission_id: str | None,
    tool_name: str,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any],
    was_rejection: bool,
    latency_ms: int,
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO mock_tool_call_logs
            (session_id, task_id, submission_id, tool_name, request_payload, response_payload, was_enforcement_rejection, latency_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                task_id,
                submission_id,
                tool_name,
                json.dumps(request_payload),
                json.dumps(response_payload),
                1 if was_rejection else 0,
                latency_ms,
            ),
        )


def get_retrieved_evidence_ids(conn: sqlite3.Connection, session_id: str, task_id: str) -> set[str]:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT response_payload FROM mock_tool_call_logs WHERE session_id = ? AND task_id = ?",
        (session_id, task_id),
    )
    rows = cursor.fetchall()
    retrieved: set[str] = set()
    for row in rows:
        try:
            resp = json.loads(row["response_payload"])
        except Exception:
            continue
        for item in resp.get("results", []):
            if isinstance(item, dict) and "id" in item:
                retrieved.add(item["id"])
        doc = resp.get("document")
        if isinstance(doc, dict) and "id" in doc:
            retrieved.add(doc["id"])
        cust = resp.get("customer")
        if isinstance(cust, dict) and "id" in cust:
            retrieved.add(cust["id"])
        for tx in resp.get("transactions", []):
            if isinstance(tx, dict):
                if "id" in tx:
                    retrieved.add(tx["id"])
                if "invoice_id" in tx:
                    retrieved.add(tx["invoice_id"])
        sub = resp.get("subscription")
        if isinstance(sub, dict) and "id" in sub:
            retrieved.add(sub["id"])
        for c in resp.get("cases", []):
            if isinstance(c, dict):
                if "case_id" in c:
                    retrieved.add(c["case_id"])
                if "id" in c:
                    retrieved.add(c["id"])
                for eid in c.get("evidence_used", []):
                    retrieved.add(eid)
        tx_ref = resp.get("transaction")
        if isinstance(tx_ref, dict) and "id" in tx_ref:
            retrieved.add(tx_ref["id"])
        if resp.get("policy_ref"):
            retrieved.add(resp["policy_ref"])
    return retrieved


# =============================================================================
# Tool Execution Logic (Parity with ToolService)
# =============================================================================


def run_read_tool(tool_name: str, world_state: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "search_knowledge":
        query = payload["query"]
        top_k = payload.get("top_k", 5)
        all_docs = []
        seen_ids = set()
        for pol in world_state.get("policies", []):
            if isinstance(pol, dict) and pol.get("id") and pol["id"] not in seen_ids:
                seen_ids.add(pol["id"])
                all_docs.append(pol)
        for doc in world_state.get("documents", []):
            if isinstance(doc, dict) and doc.get("id") and doc["id"] not in seen_ids:
                seen_ids.add(doc["id"])
                all_docs.append(doc)

        query_tokens = [w.lower() for w in query.split() if len(w) > 1]
        scored_docs = []
        for d in all_docs:
            title = d.get("title", "").lower()
            content = d.get("content", "").lower()
            doc_id = d.get("id", "").lower()
            category = d.get("category", "").lower()
            score = 0
            if query.lower() in title or query.lower() in content:
                score += 10
            for token in query_tokens:
                if token in doc_id:
                    score += 8
                if token in title:
                    score += 5
                if token in category:
                    score += 3
                if token in content:
                    score += 1
            if score > 0 or not query_tokens:
                full_content = d.get("content", "")
                snippet = full_content[:300] + ("..." if len(full_content) > 300 else "")
                scored_docs.append(
                    (
                        score,
                        parse_iso(d.get("updated_at", "1970-01-01T00:00:00Z")),
                        d["id"],
                        {
                            "id": d["id"],
                            "title": d.get("title", ""),
                            "snippet": snippet,
                            "updated_at": d.get("updated_at", ""),
                            "category": d.get("category", "general"),
                        },
                    )
                )
        scored_docs.sort(key=lambda x: (-x[0], -x[1].timestamp(), x[2]))
        return {"results": [item[3] for item in scored_docs[:top_k]]}

    elif tool_name == "get_document":
        doc_id = payload["document_id"]
        for pol in world_state.get("policies", []):
            if pol.get("id") == doc_id:
                return {"document": pol}
        for doc in world_state.get("documents", []):
            if doc.get("id") == doc_id:
                return {"document": doc}
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "DOCUMENT_NOT_FOUND", "message": f"Document '{doc_id}' not found in task knowledge base."},
        )

    elif tool_name == "get_customer":
        cust_id = payload["customer_id"]
        for cust in world_state.get("customers", []):
            if cust.get("id") == cust_id:
                return {"customer": cust}
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "CUSTOMER_NOT_FOUND", "message": f"Customer '{cust_id}' not found in account records."},
        )

    elif tool_name == "get_transactions":
        cust_id = payload["customer_id"]
        cust_exists = any(c.get("id") == cust_id for c in world_state.get("customers", []))
        if not cust_exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "CUSTOMER_NOT_FOUND", "message": f"Customer '{cust_id}' not found."},
            )
        start_date = payload.get("start_date")
        end_date = payload.get("end_date")
        start_dt = None
        end_dt = None
        if start_date:
            try:
                start_dt = parse_iso(start_date)
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail={"error": "INVALID_DATE_FORMAT", "message": f"Invalid start_date '{start_date}': {e}"},
                )
        if end_date:
            try:
                end_dt = parse_iso(end_date)
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail={"error": "INVALID_DATE_FORMAT", "message": f"Invalid end_date '{end_date}': {e}"},
                )
        if start_dt and end_dt and start_dt > end_dt:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error": "INVALID_DATE_RANGE",
                    "message": f"start_date '{start_date}' cannot be after end_date '{end_date}'.",
                },
            )
        txs = [t for t in world_state.get("transactions", []) if t.get("customer_id") == cust_id]
        filtered = []
        for t in txs:
            t_dt = parse_iso(t.get("date", "1970-01-01T00:00:00Z"))
            if start_dt and t_dt < start_dt:
                continue
            if end_dt and t_dt > end_dt:
                continue
            filtered.append(t)
        filtered.sort(key=lambda t: parse_iso(t.get("date", "1970-01-01T00:00:00Z")), reverse=True)
        return {"transactions": filtered}

    elif tool_name == "get_subscription":
        cust_id = payload["customer_id"]
        cust_exists = any(c.get("id") == cust_id for c in world_state.get("customers", []))
        if not cust_exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "CUSTOMER_NOT_FOUND", "message": f"Customer '{cust_id}' not found."},
            )
        for sub in world_state.get("subscriptions", []):
            if sub.get("customer_id") == cust_id:
                return {"subscription": sub}
        return {"subscription": None}

    elif tool_name == "get_previous_cases":
        cust_id = payload["customer_id"]
        limit = payload.get("limit", 5)
        cust_exists = any(c.get("id") == cust_id for c in world_state.get("customers", []))
        if not cust_exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "CUSTOMER_NOT_FOUND", "message": f"Customer '{cust_id}' not found."},
            )
        cases = []
        for c in world_state.get("historical_cases", []):
            if c.get("customer_id") == cust_id:
                clean_case = {k: v for k, v in c.items() if k not in ("was_correct", "_eval_ground_truth")}
                cases.append(clean_case)
        cases.sort(key=lambda c: parse_iso(c.get("date", "1970-01-01T00:00:00Z")), reverse=True)
        return {"cases": cases[:limit]}

    raise HTTPException(status_code=400, detail={"error": "UNKNOWN_TOOL", "message": f"Unknown tool '{tool_name}'"})


def run_action_tool(
    tool_name: str,
    world_state: dict[str, Any],
    payload: dict[str, Any],
    retrieved_evidence_ids: set[str],
) -> tuple[dict[str, Any] | None, dict[str, Any], bool]:
    """Executes state-changing action tools with server-side enforcement.

    Returns (mutated_state_or_None, response_dict, was_rejection).
    """
    if tool_name == "issue_refund":
        tx_id = payload["transaction_id"]
        amount = payload["amount"]
        reason = payload["reason"]
        result = check_refund_eligibility(world_state, tx_id, amount, reason)
        if result.error == "TRANSACTION_NOT_FOUND":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "TRANSACTION_NOT_FOUND",
                    "message": f"Transaction '{tx_id}' does not exist in account records.",
                },
            )
        if not result.is_eligible:
            return (
                None,
                {
                    "error": "INELIGIBLE",
                    "reason": result.reason or "ineligible_action",
                    "policy_ref": result.policy_ref,
                },
                True,
            )

        mutated, _ = apply_action_to_world(world_state, "issue_refund", payload)
        updated_tx = next(t for t in mutated["transactions"] if t.get("id") == tx_id)
        return mutated, {"status": updated_tx.get("refund_status", "refunded"), "transaction": updated_tx}, False

    elif tool_name == "cancel_subscription":
        cust_id = payload["customer_id"]
        sub_id = payload["subscription_id"]
        cust_exists = any(c.get("id") == cust_id for c in world_state.get("customers", []) if isinstance(c, dict))
        if not cust_exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "CUSTOMER_NOT_FOUND", "message": f"Customer '{cust_id}' not found."},
            )
        result = check_cancellation_eligibility(world_state, cust_id, sub_id)
        if result.error == "SUBSCRIPTION_NOT_FOUND":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "SUBSCRIPTION_NOT_FOUND", "message": f"Subscription '{sub_id}' does not exist."},
            )
        if not result.is_eligible:
            return (
                None,
                {
                    "error": "INELIGIBLE",
                    "reason": result.reason or "ineligible_action",
                    "policy_ref": result.policy_ref,
                },
                True,
            )

        mutated, _ = apply_action_to_world(world_state, "cancel_subscription", payload)
        updated_sub = next(s for s in mutated["subscriptions"] if s["id"] == sub_id)
        return mutated, {"status": "cancelled", "subscription": updated_sub}, False

    elif tool_name == "escalate_case":
        case_id = payload["case_id"]
        team = payload["team"]
        reason = payload["reason"]
        result = check_escalation_validity(
            world_state=world_state,
            case_id=case_id,
            team=team,
            reason=reason,
            retrieved_evidence_ids=list(retrieved_evidence_ids),
        )
        if not result.is_eligible:
            return None, {"error": "INVALID_ESCALATION", "reason": result.reason or "reason_not_grounded"}, True

        mutated, _ = apply_action_to_world(
            world_state,
            "escalate_case",
            payload,
            retrieved_evidence_ids=list(retrieved_evidence_ids),
        )
        return mutated, {"status": "escalated"}, False

    elif tool_name == "request_verification":
        cust_id = payload["customer_id"]
        cust_exists = any(c.get("id") == cust_id for c in world_state.get("customers", []))
        if not cust_exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "CUSTOMER_NOT_FOUND", "message": f"Customer '{cust_id}' not found."},
            )
        mutated, _ = apply_action_to_world(world_state, "request_verification", payload)
        return mutated, {"status": "verification_requested"}, False

    raise HTTPException(status_code=400, detail={"error": "UNKNOWN_TOOL", "message": f"Unknown tool '{tool_name}'"})


# =============================================================================
# FastAPI Application & Lifecycle
# =============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Agent Arena SupportOps Mock Simulator",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "mode": "mock", "reveal_ground_truth": REVEAL_GROUND_TRUTH}


@app.post("/dev/reset")
def dev_reset(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """Simulator-only endpoint to reset active assignments, evaluations, and tool logs."""
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        with conn:
            if not authorization:
                conn.execute("DELETE FROM mock_task_assignments")
                conn.execute("DELETE FROM mock_tool_call_logs")
                conn.execute("DELETE FROM mock_submissions")
                conn.execute("DELETE FROM mock_task_evaluations")
            else:
                conn.execute("DELETE FROM mock_task_assignments WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM mock_tool_call_logs WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM mock_submissions WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM mock_task_evaluations WHERE session_id = ?", (session_id,))
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM mock_tasks")
            count = cursor.fetchone()[0]
        return {"reset": True, "tasks_available": count}
    finally:
        conn.close()


# =============================================================================
# Mock Arena Debug Dashboard Endpoints
# =============================================================================


def get_dashboard_data(conn: sqlite3.Connection) -> dict[str, Any]:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, session_id, task_id, customer_id, customer_message,
               correct, actual_resolution, expected_resolution, actual_escalation,
               expected_escalation, actual_evidence, expected_evidence, missing_evidence,
               diff_explanation, tool_calls, submitted_at
        FROM mock_task_evaluations
        ORDER BY id DESC
    """)
    rows = cursor.fetchall()
    evaluations = []
    for r in rows:
        evaluations.append(
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "task_id": r["task_id"],
                "customer_id": r["customer_id"] or "",
                "customer_message": r["customer_message"] or "",
                "correct": bool(r["correct"]),
                "actual_resolution": r["actual_resolution"] or "",
                "expected_resolution": r["expected_resolution"] or "",
                "actual_escalation": bool(r["actual_escalation"]),
                "expected_escalation": bool(r["expected_escalation"]),
                "actual_evidence": json.loads(r["actual_evidence"]) if r["actual_evidence"] else [],
                "expected_evidence": json.loads(r["expected_evidence"]) if r["expected_evidence"] else [],
                "missing_evidence": json.loads(r["missing_evidence"]) if r["missing_evidence"] else [],
                "diff_explanation": r["diff_explanation"] or "",
                "tool_calls": json.loads(r["tool_calls"]) if r["tool_calls"] else [],
                "submitted_at": r["submitted_at"],
            }
        )

    cursor.execute("SELECT COUNT(*) FROM mock_tool_call_logs")
    total_tool_calls = cursor.fetchone()[0]

    total = len(evaluations)
    passed = sum(1 for e in evaluations if e["correct"])
    failed = total - passed
    accuracy = round((passed / total * 100), 1) if total > 0 else 0.0

    return {
        "summary": {
            "total_evaluated": total,
            "passed": passed,
            "failed": failed,
            "accuracy_percent": accuracy,
            "total_tool_calls": total_tool_calls,
        },
        "failed_tasks": [e for e in evaluations if not e["correct"]],
        "passed_tasks": [e for e in evaluations if e["correct"]],
        "all_evaluations": evaluations,
    }


def render_dashboard_html(data: dict[str, Any]) -> str:
    summary = data["summary"]
    failed_tasks = data["failed_tasks"]
    passed_tasks = data["passed_tasks"]

    total = summary["total_evaluated"]
    passed = summary["passed"]
    failed = summary["failed"]
    accuracy = summary["accuracy_percent"]
    tool_calls_count = summary["total_tool_calls"]

    failed_html = ""
    if not failed_tasks:
        failed_html = """
        <div class="card-empty">
            <p>✨ <strong>No failed tasks!</strong> Either your agent passed all evaluated tasks, or no tasks have been run yet.</p>
            <p style="margin-top: 8px; font-size: 13px; color: #8b949e;">Run <code>python main.py</code> in your participant workspace to process tasks.</p>
        </div>
        """
    else:
        for f in failed_tasks:
            # Format tool pills
            tools_pills = ""
            for tc in f.get("tool_calls", []):
                tname = tc.get("tool_name", "unknown")
                lat = tc.get("latency_ms", 0)
                is_rej = tc.get("was_enforcement_rejection", 0) == 1
                badge_cls = "tool-pill rejection" if is_rej else "tool-pill"
                rej_label = " [REJECTED]" if is_rej else ""
                tools_pills += f'<span class="{badge_cls}">{tname}{rej_label} ({lat}ms)</span> '
            if not tools_pills:
                tools_pills = '<span style="color: #8b949e; font-size: 12px;">(No tools called)</span>'

            # Resolution comparison
            res_match = f["actual_resolution"] == f["expected_resolution"]
            res_status = (
                '<span class="match">✓ Matched</span>' if res_match else '<span class="mismatch">✗ Mismatch</span>'
            )

            # Escalation comparison
            esc_match = f["actual_escalation"] == f["expected_escalation"]
            esc_status = (
                '<span class="match">✓ Matched</span>' if esc_match else '<span class="mismatch">✗ Mismatch</span>'
            )

            # Evidence comparison
            missing_ev = f.get("missing_evidence", [])
            ev_status = (
                '<span class="match">✓ Complete</span>'
                if not missing_ev
                else f'<span class="mismatch">✗ Missing: {", ".join(missing_ev)}</span>'
            )

            submitted_display = f.get("submitted_at", "").replace("T", " ")[:19]

            failed_html += f"""
            <div class="fail-card">
                <div class="fail-header">
                    <div>
                        <span class="badge badge-fail">[FAIL]</span>
                        <span class="task-tag">{f["task_id"]}</span>
                        <span class="meta-tag">• Customer: {f["customer_id"]}</span>
                    </div>
                    <div class="meta-tag">Submitted: {submitted_display} UTC</div>
                </div>
                
                <div class="diff-alert">
                    <strong>Diff Explanation:</strong> {f["diff_explanation"]}
                </div>

                <div class="inquiry-box">
                    <strong>Customer ({f["customer_id"]}):</strong> "{f["customer_message"]}"
                </div>

                <table class="diff-table">
                    <thead>
                        <tr>
                            <th style="width: 25%;">Field</th>
                            <th style="width: 30%;">Expected (Ground Truth)</th>
                            <th style="width: 30%;">Agent Output</th>
                            <th style="width: 15%;">Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr>
                            <td>Decision Resolution</td>
                            <td><code>{f["expected_resolution"]}</code></td>
                            <td><code>{f["actual_resolution"]}</code></td>
                            <td>{res_status}</td>
                        </tr>
                        <tr>
                            <td>Escalation Required</td>
                            <td><code>{f["expected_escalation"]}</code></td>
                            <td><code>{f["actual_escalation"]}</code></td>
                            <td>{esc_status}</td>
                        </tr>
                        <tr>
                            <td>Evidence Citations</td>
                            <td><code>{", ".join(f.get("expected_evidence", [])) or "[]"}</code></td>
                            <td><code>{", ".join(f.get("actual_evidence", [])) or "[]"}</code></td>
                            <td>{ev_status}</td>
                        </tr>
                    </tbody>
                </table>

                <div class="tools-trace">
                    <span class="trace-label">Tools Executed:</span>
                    {tools_pills}
                </div>
            </div>
            """

    # Passed tasks rows & cards
    passed_rows = ""
    passed_cards = ""
    for p in passed_tasks:
        sub_time = p.get("submitted_at", "").replace("T", " ")[:19]
        ev_count = len(p.get("actual_evidence", []))
        tool_count = len(p.get("tool_calls", []))
        passed_rows += f"""
        <tr>
            <td><span class="badge badge-pass">[PASS]</span> <code>{p["task_id"]}</code></td>
            <td><code>{p["customer_id"]}</code></td>
            <td><code>{p["actual_resolution"]}</code></td>
            <td>{ev_count} item(s)</td>
            <td>{tool_count} call(s)</td>
            <td style="color: #8b949e;">{sub_time}</td>
        </tr>
        """

        tools_pills = ""
        for tc in p.get("tool_calls", []):
            tname = tc.get("tool_name", "unknown")
            lat = tc.get("latency_ms", 0)
            is_rej = tc.get("was_enforcement_rejection", 0) == 1
            badge_cls = "tool-pill rejection" if is_rej else "tool-pill"
            rej_label = " [REJECTED]" if is_rej else ""
            tools_pills += f'<span class="{badge_cls}">{tname}{rej_label} ({lat}ms)</span> '
        if not tools_pills:
            tools_pills = '<span style="color: #8b949e; font-size: 12px;">(No tools called)</span>'

        passed_cards += f"""
        <div class="pass-card">
            <div class="pass-header">
                <div>
                    <span class="badge badge-pass">[PASS]</span>
                    <span class="task-tag">{p["task_id"]}</span>
                    <span class="meta-tag">• Customer: {p["customer_id"]}</span>
                </div>
                <div class="meta-tag">Submitted: {sub_time} UTC</div>
            </div>

            <div class="inquiry-box">
                <strong>Customer ({p["customer_id"]}):</strong> "{p["customer_message"]}"
            </div>

            <table class="diff-table">
                <thead>
                    <tr>
                        <th style="width: 25%;">Field</th>
                        <th style="width: 35%;">Ground Truth</th>
                        <th style="width: 35%;">Agent Output</th>
                        <th style="width: 15%;">Status</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td>Resolution</td>
                        <td><code>{p["expected_resolution"]}</code></td>
                        <td><code>{p["actual_resolution"]}</code></td>
                        <td><span class="match">✓ Matched</span></td>
                    </tr>
                    <tr>
                        <td>Escalation Required</td>
                        <td><code>{p["expected_escalation"]}</code></td>
                        <td><code>{p["actual_escalation"]}</code></td>
                        <td><span class="match">✓ Matched</span></td>
                    </tr>
                    <tr>
                        <td>Evidence Citations</td>
                        <td><code>{", ".join(p.get("expected_evidence", [])) or "[]"}</code></td>
                        <td><code>{", ".join(p.get("actual_evidence", [])) or "[]"}</code></td>
                        <td><span class="match">✓ Complete</span></td>
                    </tr>
                </tbody>
            </table>

            <div class="tools-trace">
                <span class="trace-label">Tools Executed:</span>
                {tools_pills}
            </div>
        </div>
        """

    if not passed_rows:
        passed_rows = '<tr><td colspan="7" style="text-align: center; color: #8b949e; padding: 16px;">No passed tasks recorded yet.</td></tr>'
    if not passed_cards:
        passed_cards = '<div class="card-empty"><p>No passed tasks recorded yet.</p></div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Arena SupportOps — Mock Debugger</title>
<style>
  :root {{
    --bg: #0d1117;
    --surface: #161b22;
    --surface-hover: #1c2128;
    --border: #30363d;
    --text: #c9d1d9;
    --text-muted: #8b949e;
    --accent: #58a6ff;
    --pass: #3fb950;
    --pass-bg: rgba(63, 185, 80, 0.15);
    --fail: #f85149;
    --fail-bg: rgba(248, 81, 73, 0.15);
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background-color: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    padding: 24px;
    line-height: 1.5;
  }}
  .container {{ max-width: 1200px; margin: 0 auto; }}
  .header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 16px;
    padding-bottom: 20px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 24px;
  }}
  .title-group h1 {{ font-size: 22px; font-weight: 600; color: #f0f6fc; display: flex; align-items: center; gap: 8px; }}
  .title-group p {{ font-size: 13px; color: var(--text-muted); margin-top: 4px; }}
  .badge {{
    display: inline-flex;
    align-items: center;
    padding: 2px 7px;
    font-size: 11px;
    font-weight: 600;
    border-radius: 4px;
    border: 1px solid var(--border);
  }}
  .badge-online {{ background: var(--pass-bg); color: var(--pass); border-color: rgba(63, 185, 80, 0.3); }}
  .badge-fail {{ background: var(--fail-bg); color: var(--fail); border-color: rgba(248, 81, 73, 0.3); }}
  .badge-pass {{ background: var(--pass-bg); color: var(--pass); border-color: rgba(63, 185, 80, 0.3); }}
  .controls {{ display: flex; align-items: center; gap: 10px; }}
  button {{
    background: var(--surface);
    color: var(--text);
    border: 1px solid var(--border);
    padding: 6px 12px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 12px;
    font-weight: 500;
  }}
  button:hover {{ background: var(--surface-hover); border-color: #8b949e; }}
  .btn-danger {{ color: #f85149; border-color: rgba(248, 81, 73, 0.4); }}
  .btn-danger:hover {{ background: var(--fail-bg); }}
  
  .stats-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 14px;
    margin-bottom: 28px;
  }}
  .stat-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
  }}
  .stat-card .label {{ font-size: 11px; color: var(--text-muted); text-transform: uppercase; font-weight: 600; letter-spacing: 0.5px; }}
  .stat-card .value {{ font-size: 26px; font-weight: 700; color: #f0f6fc; margin-top: 4px; }}
  
  .section-title {{ font-size: 17px; font-weight: 600; margin-bottom: 14px; display: flex; align-items: center; gap: 8px; color: #f0f6fc; }}
  
  .card-empty {{
    background: var(--surface);
    border: 1px dashed var(--border);
    border-radius: 8px;
    padding: 32px;
    text-align: center;
    color: var(--text-muted);
    font-size: 14px;
    margin-bottom: 24px;
  }}

  .fail-card {{
    background: var(--surface);
    border: 1px solid rgba(248, 81, 73, 0.35);
    border-left: 4px solid var(--fail);
    border-radius: 8px;
    padding: 18px;
    margin-bottom: 16px;
  }}
  .fail-header {{
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 12px;
  }}
  .pass-card {{
    background: var(--surface);
    border: 1px solid rgba(63, 185, 80, 0.35);
    border-left: 4px solid var(--pass);
    border-radius: 8px;
    padding: 18px;
    margin-bottom: 16px;
  }}
  .pass-header {{
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 12px;
  }}
  .task-tag {{ font-family: monospace; font-size: 14px; font-weight: 600; color: #f0f6fc; margin-left: 4px; }}
  .meta-tag {{ font-size: 12px; color: var(--text-muted); }}
  
  .diff-alert {{
    background: var(--fail-bg);
    border: 1px solid rgba(248, 81, 73, 0.3);
    border-radius: 6px;
    padding: 10px 14px;
    color: #ff7b72;
    font-size: 13px;
    margin-bottom: 12px;
  }}

  .inquiry-box {{
    background: #0d1117;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px 12px;
    font-size: 13px;
    color: #8b949e;
    margin-bottom: 12px;
  }}
  .inquiry-box strong {{ color: var(--text); }}

  .diff-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
    margin-bottom: 12px;
  }}
  .diff-table th, .diff-table td {{
    padding: 7px 10px;
    border: 1px solid var(--border);
    text-align: left;
  }}
  .diff-table th {{ background: #161b22; color: var(--text-muted); font-weight: 500; }}
  .mismatch {{ color: #f85149; font-weight: 600; }}
  .match {{ color: #3fb950; font-weight: 600; }}

  .tools-trace {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
    margin-top: 10px;
  }}
  .trace-label {{ font-size: 12px; color: var(--text-muted); font-weight: 500; }}
  .tool-pill {{
    font-family: monospace;
    font-size: 11px;
    padding: 2px 7px;
    border-radius: 4px;
    background: #21262d;
    border: 1px solid var(--border);
    color: var(--accent);
  }}
  .tool-pill.rejection {{ background: var(--fail-bg); color: #f85149; border-color: rgba(248, 81, 73, 0.4); }}

  details {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px 18px;
    margin-top: 24px;
  }}
  details summary {{
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    color: var(--text);
  }}
  .pass-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
    margin-top: 12px;
  }}
  .pass-table th, .pass-table td {{
    padding: 8px 10px;
    border-bottom: 1px solid var(--border);
    text-align: left;
  }}
  .pass-table th {{ color: var(--text-muted); font-weight: 500; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="title-group">
      <h1>🛠️ Mock Simulator — Agent Debugger</h1>
      <p>Local Ground Truth Verification & Evaluation Debugger • No Auth Token Required</p>
    </div>
    <div class="controls">
      <span class="badge badge-online">🟢 Online (Practice Mode)</span>
      <button id="toggle-auto">⚡ Auto-Refresh: ON</button>
      <button onclick="location.reload()">🔄 Refresh</button>
      <button class="btn-danger" onclick="resetHistory()">🗑️ Reset History</button>
    </div>
  </div>

  <div class="stats-grid">
    <div class="stat-card">
      <div class="label">Evaluations Attempted</div>
      <div class="value">{total} / 30</div>
    </div>
    <div class="stat-card">
      <div class="label">Tasks Passed</div>
      <div class="value" style="color: var(--pass);">{passed}</div>
    </div>
    <div class="stat-card">
      <div class="label">Tasks Failed</div>
      <div class="value" style="color: var(--fail);">{failed}</div>
    </div>
    <div class="stat-card">
      <div class="label">Score Report</div>
      <div class="value" style="font-size: 22px;">{passed}/{total} ({accuracy}%)</div>
    </div>
    <div class="stat-card">
      <div class="label">Total Tool Calls</div>
      <div class="value">{tool_calls_count}</div>
    </div>
  </div>

  <div class="section-title">
    <span>🚨 Failed Tasks ({failed}) — Debug Trace & Ground Truth Mismatches</span>
  </div>

  {failed_html}

  <details open style="margin-top: 24px;">
    <summary style="font-size: 16px; font-weight: 600; cursor: pointer; color: var(--text);">
      ✅ Passed Tasks ({passed}) — Inspect Successful Resolutions & Traces
    </summary>
    <div style="margin-top: 14px;">
      {passed_cards}
    </div>
  </details>
</div>

<script>
  let autoRefresh = localStorage.getItem('mock_auto_refresh') !== 'false';
  const toggleBtn = document.getElementById('toggle-auto');
  function updateToggle() {{
    toggleBtn.textContent = autoRefresh ? '⚡ Auto-Refresh: ON' : '⏸️ Auto-Refresh: OFF';
    toggleBtn.style.color = autoRefresh ? '#3fb950' : '#8b949e';
  }}
  toggleBtn.onclick = () => {{
    autoRefresh = !autoRefresh;
    localStorage.setItem('mock_auto_refresh', autoRefresh);
    updateToggle();
  }};
  updateToggle();
  setInterval(() => {{
    if (autoRefresh) location.reload();
  }}, 3000);

  function resetHistory() {{
    if (confirm("Reset all task evaluations and tool call history in the mock simulator?")) {{
      fetch("/dev/reset", {{method: "POST"}}).then(() => location.reload());
    }}
  }}
</script>
</body>
</html>
"""


@app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard() -> HTMLResponse:
    """Exposes the standalone visual debug dashboard for evaluating local agent performance."""
    conn = get_db_connection()
    try:
        data = get_dashboard_data(conn)
        html_content = render_dashboard_html(data)
        return HTMLResponse(content=html_content)
    finally:
        conn.close()


@app.get("/api/dashboard")
def get_dashboard_api() -> dict[str, Any]:
    """Programmatic JSON endpoint returning evaluation breakdown and failed tasks."""
    conn = get_db_connection()
    try:
        return get_dashboard_data(conn)
    finally:
        conn.close()


@app.get("/", response_class=RedirectResponse)
def root_redirect() -> RedirectResponse:
    """Redirects simulator root traffic to the visual debug dashboard."""
    return RedirectResponse(url="/dashboard")


# --- Tool Endpoints ---


@app.post("/tools/search_knowledge")
def search_knowledge(
    req: SearchKnowledgeRequest,
    authorization: str | None = Header(default=None),
    x_task_id: str | None = Header(default=None),
) -> dict[str, Any]:
    t0 = time.perf_counter()
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        _, task_id, world_state, submission_id = get_active_assignment(conn, session_id, task_id=x_task_id)
        resp = run_read_tool("search_knowledge", world_state, req.model_dump())
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        log_tool_call(
            conn, session_id, task_id, submission_id, "search_knowledge", req.model_dump(), resp, False, latency_ms
        )
        return resp
    finally:
        conn.close()


@app.post("/tools/get_document")
def get_document(
    req: GetDocumentRequest,
    authorization: str | None = Header(default=None),
    x_task_id: str | None = Header(default=None),
) -> dict[str, Any]:
    t0 = time.perf_counter()
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        _, task_id, world_state, submission_id = get_active_assignment(conn, session_id, task_id=x_task_id)
        resp = run_read_tool("get_document", world_state, req.model_dump())
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        log_tool_call(
            conn, session_id, task_id, submission_id, "get_document", req.model_dump(), resp, False, latency_ms
        )
        return resp
    finally:
        conn.close()


@app.post("/tools/get_customer")
def get_customer(
    req: GetCustomerRequest,
    authorization: str | None = Header(default=None),
    x_task_id: str | None = Header(default=None),
) -> dict[str, Any]:
    t0 = time.perf_counter()
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        _, task_id, world_state, submission_id = get_active_assignment(conn, session_id, task_id=x_task_id)
        resp = run_read_tool("get_customer", world_state, req.model_dump())
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        log_tool_call(
            conn, session_id, task_id, submission_id, "get_customer", req.model_dump(), resp, False, latency_ms
        )
        return resp
    finally:
        conn.close()


@app.post("/tools/get_transactions")
def get_transactions(
    req: GetTransactionsRequest,
    authorization: str | None = Header(default=None),
    x_task_id: str | None = Header(default=None),
) -> dict[str, Any]:
    t0 = time.perf_counter()
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        _, task_id, world_state, submission_id = get_active_assignment(conn, session_id, task_id=x_task_id)
        resp = run_read_tool("get_transactions", world_state, req.model_dump())
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        log_tool_call(
            conn, session_id, task_id, submission_id, "get_transactions", req.model_dump(), resp, False, latency_ms
        )
        return resp
    finally:
        conn.close()


@app.post("/tools/get_subscription")
def get_subscription(
    req: GetSubscriptionRequest,
    authorization: str | None = Header(default=None),
    x_task_id: str | None = Header(default=None),
) -> dict[str, Any]:
    t0 = time.perf_counter()
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        _, task_id, world_state, submission_id = get_active_assignment(conn, session_id, task_id=x_task_id)
        resp = run_read_tool("get_subscription", world_state, req.model_dump())
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        log_tool_call(
            conn, session_id, task_id, submission_id, "get_subscription", req.model_dump(), resp, False, latency_ms
        )
        return resp
    finally:
        conn.close()


@app.post("/tools/get_previous_cases")
def get_previous_cases(
    req: GetPreviousCasesRequest,
    authorization: str | None = Header(default=None),
    x_task_id: str | None = Header(default=None),
) -> dict[str, Any]:
    t0 = time.perf_counter()
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        _, task_id, world_state, submission_id = get_active_assignment(conn, session_id, task_id=x_task_id)
        resp = run_read_tool("get_previous_cases", world_state, req.model_dump())
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        log_tool_call(
            conn, session_id, task_id, submission_id, "get_previous_cases", req.model_dump(), resp, False, latency_ms
        )
        return resp
    finally:
        conn.close()


@app.post("/tools/issue_refund")
def issue_refund(
    req: IssueRefundRequest,
    authorization: str | None = Header(default=None),
    x_task_id: str | None = Header(default=None),
) -> dict[str, Any]:
    t0 = time.perf_counter()
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        assignment_id, task_id, world_state, submission_id = get_active_assignment(conn, session_id, task_id=x_task_id)
        retrieved_ids = get_retrieved_evidence_ids(conn, session_id, task_id)
        mutated, resp, was_rejection = run_action_tool("issue_refund", world_state, req.model_dump(), retrieved_ids)
        if mutated is not None and not was_rejection:
            update_world_state(conn, assignment_id, mutated)
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        log_tool_call(
            conn, session_id, task_id, submission_id, "issue_refund", req.model_dump(), resp, was_rejection, latency_ms
        )
        return resp
    finally:
        conn.close()


@app.post("/tools/cancel_subscription")
def cancel_subscription(
    req: CancelSubscriptionRequest,
    authorization: str | None = Header(default=None),
    x_task_id: str | None = Header(default=None),
) -> dict[str, Any]:
    t0 = time.perf_counter()
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        assignment_id, task_id, world_state, submission_id = get_active_assignment(conn, session_id, task_id=x_task_id)
        retrieved_ids = get_retrieved_evidence_ids(conn, session_id, task_id)
        mutated, resp, was_rejection = run_action_tool(
            "cancel_subscription", world_state, req.model_dump(), retrieved_ids
        )
        if mutated is not None and not was_rejection:
            update_world_state(conn, assignment_id, mutated)
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        log_tool_call(
            conn,
            session_id,
            task_id,
            submission_id,
            "cancel_subscription",
            req.model_dump(),
            resp,
            was_rejection,
            latency_ms,
        )
        return resp
    finally:
        conn.close()


@app.post("/tools/escalate_case")
def escalate_case(
    req: EscalateCaseRequest,
    authorization: str | None = Header(default=None),
    x_task_id: str | None = Header(default=None),
) -> dict[str, Any]:
    t0 = time.perf_counter()
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        assignment_id, task_id, world_state, submission_id = get_active_assignment(conn, session_id, task_id=x_task_id)
        retrieved_ids = get_retrieved_evidence_ids(conn, session_id, task_id)
        mutated, resp, was_rejection = run_action_tool("escalate_case", world_state, req.model_dump(), retrieved_ids)
        if mutated is not None and not was_rejection:
            update_world_state(conn, assignment_id, mutated)
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        log_tool_call(
            conn, session_id, task_id, submission_id, "escalate_case", req.model_dump(), resp, was_rejection, latency_ms
        )
        return resp
    finally:
        conn.close()


@app.post("/tools/request_verification")
def request_verification(
    req: RequestVerificationRequest,
    authorization: str | None = Header(default=None),
    x_task_id: str | None = Header(default=None),
) -> dict[str, Any]:
    t0 = time.perf_counter()
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        assignment_id, task_id, world_state, submission_id = get_active_assignment(conn, session_id, task_id=x_task_id)
        retrieved_ids = get_retrieved_evidence_ids(conn, session_id, task_id)
        mutated, resp, was_rejection = run_action_tool(
            "request_verification", world_state, req.model_dump(), retrieved_ids
        )
        if mutated is not None and not was_rejection:
            update_world_state(conn, assignment_id, mutated)
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        log_tool_call(
            conn,
            session_id,
            task_id,
            submission_id,
            "request_verification",
            req.model_dump(),
            resp,
            was_rejection,
            latency_ms,
        )
        return resp
    finally:
        conn.close()


# --- Task Flow Endpoints ---


@app.post("/task/start", response_model=TaskStartResponse)
def start_task(authorization: str | None = Header(default=None)) -> TaskStartResponse:
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # Check if there is an active submission in progress
        cursor.execute(
            "SELECT submission_id FROM mock_submissions WHERE session_id = ? AND status = 'in_progress' ORDER BY started_at DESC LIMIT 1",
            (session_id,),
        )
        sub_row = cursor.fetchone()
        active_sub_id = sub_row["submission_id"] if sub_row else None

        # Find next task not yet assigned to this submission (or session)
        if active_sub_id:
            cursor.execute(
                """
                SELECT t.task_id, t.input_payload, t.world_state_seed
                FROM mock_tasks t
                WHERE t.task_id NOT IN (
                    SELECT task_id FROM mock_task_assignments WHERE submission_id = ?
                )
                ORDER BY t.task_id ASC LIMIT 1
                """,
                (active_sub_id,),
            )
        else:
            cursor.execute(
                """
                SELECT t.task_id, t.input_payload, t.world_state_seed
                FROM mock_tasks t
                WHERE t.task_id NOT IN (
                    SELECT task_id FROM mock_task_assignments WHERE session_id = ? AND submission_id IS NULL
                )
                ORDER BY t.task_id ASC LIMIT 1
                """,
                (session_id,),
            )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "NO_MORE_TASKS",
                    "message": "All tasks have been assigned. Finalize submission or call /dev/reset.",
                },
            )

        task_id = row["task_id"]
        input_payload = json.loads(row["input_payload"])
        world_seed = json.loads(row["world_state_seed"])

        # Create fresh working copy
        now_iso = datetime.now(UTC).isoformat()
        with conn:
            conn.execute(
                """
                INSERT INTO mock_task_assignments (session_id, task_id, submission_id, assigned_at, world_runtime_state)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, task_id, active_sub_id, now_iso, json.dumps(world_seed)),
            )

        return TaskStartResponse(
            task_id=task_id,
            customer_message=input_payload.get("customer_message", ""),
            customer_id=input_payload.get("customer_id", ""),
        )
    finally:
        conn.close()


@app.post(
    "/task/submit", response_model=TaskSubmitResponse | TaskSubmitPracticeResponse, response_model_exclude_none=True
)
def submit_task(
    req: TaskSubmitRequest, authorization: str | None = Header(default=None)
) -> TaskSubmitResponse | TaskSubmitPracticeResponse:
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        assignment_id, active_task_id, runtime_state, submission_id = get_active_assignment(
            conn, session_id, task_id=req.task_id
        )
        if active_task_id != req.task_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "TASK_ID_MISMATCH",
                    "message": f"Active task is '{active_task_id}', cannot submit '{req.task_id}'.",
                },
            )

        # Production response: strictly received and task_id
        if not REVEAL_GROUND_TRUTH:
            return TaskSubmitResponse(received=True, task_id=req.task_id)

        # Mock reveal practice mode: fetch privileged ground truth
        cursor = conn.cursor()
        cursor.execute("SELECT ground_truth_privileged FROM mock_tasks WHERE task_id = ?", (req.task_id,))
        row = cursor.fetchone()
        if not row:
            return TaskSubmitResponse(received=True, task_id=req.task_id)

        gt = json.loads(row["ground_truth_privileged"])
        exp_res = gt.get("expected_resolution")
        must_esc = gt.get("must_escalate", False)
        req_ev = gt.get("required_evidence", [])

        # Evaluation checks
        resolution_correct = req.decision.resolution == exp_res
        escalation_correct = req.decision.escalation_required == must_esc
        evidence_set = set(req.evidence)
        missing_evidence = [e for e in req_ev if e not in evidence_set]

        correct = resolution_correct and escalation_correct and len(missing_evidence) == 0

        diff_parts = []
        if not resolution_correct:
            diff_parts.append(f"Resolution mismatch: expected '{exp_res}', got '{req.decision.resolution}'.")
        if not escalation_correct:
            diff_parts.append(f"Escalation mismatch: expected {must_esc}, got {req.decision.escalation_required}.")
        if missing_evidence:
            diff_parts.append(f"Missing required evidence IDs: {missing_evidence}.")
        if correct:
            diff_parts.append("Decision, escalation flag, and required evidence match ground truth.")

        diff_str = " ".join(diff_parts)

        # Record evaluation in mock_task_evaluations
        cursor.execute("SELECT input_payload FROM mock_tasks WHERE task_id = ?", (req.task_id,))
        task_meta = cursor.fetchone()
        input_data = json.loads(task_meta["input_payload"]) if task_meta else {}
        customer_id = input_data.get("customer_id", "")
        customer_message = input_data.get("customer_message", "")

        cursor.execute(
            """
            SELECT tool_name, was_enforcement_rejection, latency_ms
            FROM mock_tool_call_logs
            WHERE session_id = ? AND task_id = ?
            ORDER BY id ASC
            """,
            (session_id, req.task_id),
        )
        tool_logs = [dict(r) for r in cursor.fetchall()]

        now_iso = datetime.now(UTC).isoformat()
        with conn:
            conn.execute(
                """
                INSERT INTO mock_task_evaluations
                (session_id, task_id, customer_id, customer_message,
                 correct, actual_resolution, expected_resolution, actual_escalation,
                 expected_escalation, actual_evidence, expected_evidence, missing_evidence,
                 diff_explanation, tool_calls, submitted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    req.task_id,
                    customer_id,
                    customer_message,
                    1 if correct else 0,
                    req.decision.resolution,
                    exp_res,
                    1 if req.decision.escalation_required else 0,
                    1 if must_esc else 0,
                    json.dumps(req.evidence),
                    json.dumps(req_ev),
                    json.dumps(missing_evidence),
                    diff_str,
                    json.dumps(tool_logs),
                    now_iso,
                ),
            )
            # Update active submission per_task_results if part of a submission
            if submission_id:
                cursor.execute(
                    "SELECT per_task_results FROM mock_submissions WHERE submission_id = ?", (submission_id,)
                )
                sub_row = cursor.fetchone()
                if sub_row:
                    current_results = json.loads(sub_row["per_task_results"]) if sub_row["per_task_results"] else []
                    current_results.append({"task_id": req.task_id, "correct": correct})
                    conn.execute(
                        "UPDATE mock_submissions SET per_task_results = ? WHERE submission_id = ?",
                        (json.dumps(current_results), submission_id),
                    )

        return TaskSubmitPracticeResponse(
            received=True,
            task_id=req.task_id,
            correct=correct,
            expected_resolution=exp_res,
            expected_evidence=req_ev,
            your_evidence=req.evidence,
            diff_explanation=diff_str,
        )
    finally:
        conn.close()


# --- Submission Lifecycle Endpoints ---


@app.post("/submission/start", response_model=SubmissionStartResponse)
def start_submission(authorization: str | None = Header(default=None)) -> SubmissionStartResponse:
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # Check if an active submission is already in progress
        cursor.execute(
            "SELECT submission_id FROM mock_submissions WHERE session_id = ? AND status = 'in_progress' ORDER BY started_at DESC LIMIT 1",
            (session_id,),
        )
        existing = cursor.fetchone()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "ACTIVE_SUBMISSION_EXISTS",
                    "message": f"Active submission '{existing['submission_id']}' is already in progress. Finalize it first.",
                    "submission_id": existing["submission_id"],
                },
            )

        cursor.execute("SELECT COUNT(*) FROM mock_tasks")
        total_tasks = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM mock_submissions WHERE session_id = ?", (session_id,))
        sub_count = cursor.fetchone()[0]

        cursor.execute("SELECT task_id, input_payload, world_state_seed FROM mock_tasks ORDER BY task_id ASC")
        task_rows = cursor.fetchall()

        sub_id = hashlib.sha256(f"{session_id}-{sub_count + 1}-{time.time()}".encode("utf-8")).hexdigest()[:32]
        now_iso = datetime.now(UTC).isoformat()
        task_items = []
        with conn:
            conn.execute(
                """
                INSERT INTO mock_submissions (submission_id, session_id, attempt_number, status, started_at, per_task_results)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (sub_id, session_id, sub_count + 1, "in_progress", now_iso, json.dumps([])),
            )
            for row in task_rows:
                tid = row["task_id"]
                inp = json.loads(row["input_payload"])
                w_seed = json.loads(row["world_state_seed"])
                conn.execute(
                    """
                    INSERT INTO mock_task_assignments (session_id, task_id, submission_id, assigned_at, world_runtime_state)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (session_id, tid, sub_id, now_iso, json.dumps(w_seed)),
                )
                task_items.append(
                    TaskStartResponse(
                        task_id=tid,
                        customer_id=inp.get("customer_id", ""),
                        customer_message=inp.get("customer_message", ""),
                    )
                )

        return SubmissionStartResponse(
            submission_id=sub_id,
            attempt_number=sub_count + 1,
            tasks_total=total_tasks,
            tasks=task_items,
        )
    finally:
        conn.close()


@app.post("/submission/{submission_id}/submit", response_model=BatchSubmissionSubmitResponse)
@app.post("/submission/{submission_id}/submit_batch", response_model=BatchSubmissionSubmitResponse)
def submit_submission_batch(
    submission_id: str,
    req: BatchSubmissionSubmitRequest,
    authorization: str | None = Header(default=None),
) -> BatchSubmissionSubmitResponse:
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status FROM mock_submissions WHERE submission_id = ? AND session_id = ?",
            (submission_id, session_id),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(
                status_code=404,
                detail={"error": "SUBMISSION_NOT_FOUND", "message": f"Submission '{submission_id}' not found."},
            )
        if row["status"] != "in_progress":
            raise HTTPException(
                status_code=409,
                detail={"error": "SUBMISSION_CLOSED", "message": f"Submission is already {row['status']}."},
            )

        cursor.execute("SELECT COUNT(*) FROM mock_tasks")
        total_tasks = cursor.fetchone()[0]

        if total_tasks > 0 and len(req.answers) != total_tasks:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "INCOMPLETE_BATCH_SUBMISSION",
                    "message": (
                        f"Submission requires all {total_tasks} tasks to be solved, received {len(req.answers)}. "
                        "Partial or truncated submissions are strictly forbidden in submission mode. "
                        "For testing with fewer tasks, use practice mode."
                    ),
                },
            )

        results = []
        passed_count = 0
        now_iso = datetime.now(UTC).isoformat()

        cursor.execute("SELECT task_id, input_payload, ground_truth_privileged FROM mock_tasks")
        gt_map = {
            r["task_id"]: (json.loads(r["input_payload"]), json.loads(r["ground_truth_privileged"]))
            for r in cursor.fetchall()
        }

        for item in req.answers:
            meta = gt_map.get(item.task_id)
            if not meta:
                continue
            inp, gt = meta
            exp_res = gt.get("expected_resolution")
            must_esc = gt.get("must_escalate", False)
            req_ev = gt.get("required_evidence", [])

            resolution_correct = item.decision.resolution == exp_res
            escalation_correct = item.decision.escalation_required == must_esc
            ev_set = set(item.evidence)
            missing_ev = [e for e in req_ev if e not in ev_set]
            is_correct = resolution_correct and escalation_correct and len(missing_ev) == 0
            if is_correct:
                passed_count += 1

            diff_parts = []
            if not resolution_correct:
                diff_parts.append(f"Resolution mismatch: expected '{exp_res}', got '{item.decision.resolution}'.")
            if not escalation_correct:
                diff_parts.append(f"Escalation mismatch: expected {must_esc}, got {item.decision.escalation_required}.")
            if missing_ev:
                diff_parts.append(f"Missing required evidence IDs: {missing_ev}.")
            diff_str = " ".join(diff_parts) if diff_parts else "Ground truth matched."

            results.append({"task_id": item.task_id, "correct": is_correct, "diff": diff_str})

            conn.execute(
                """
                INSERT INTO mock_task_evaluations
                (session_id, task_id, customer_id, customer_message,
                 correct, actual_resolution, expected_resolution, actual_escalation,
                 expected_escalation, actual_evidence, expected_evidence, missing_evidence,
                 diff_explanation, tool_calls, submitted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    item.task_id,
                    inp.get("customer_id", ""),
                    inp.get("customer_message", ""),
                    1 if is_correct else 0,
                    item.decision.resolution,
                    exp_res,
                    1 if item.decision.escalation_required else 0,
                    1 if must_esc else 0,
                    json.dumps(item.evidence),
                    json.dumps(req_ev),
                    json.dumps(missing_ev),
                    diff_str,
                    json.dumps([]),
                    now_iso,
                ),
            )

        score_pct = round((passed_count / len(results)) * 100.0, 2) if results else 0.0

        with conn:
            conn.execute(
                "UPDATE mock_submissions SET status = 'completed', completed_at = ?, per_task_results = ? WHERE submission_id = ?",
                (now_iso, json.dumps(results), submission_id),
            )

        return BatchSubmissionSubmitResponse(
            submission_id=submission_id,
            status="completed",
            tasks_submitted=len(results),
            tasks_total=total_tasks,
            score_pct=score_pct,
            passed=score_pct >= 70.0,
            inter_task_durations=[],
        )
    finally:
        conn.close()


@app.post("/submission/{submission_id}/abort", response_model=SubmissionAbortResponse)
def abort_submission(submission_id: str, authorization: str | None = Header(default=None)) -> SubmissionAbortResponse:
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        now_iso = datetime.now(UTC).isoformat()
        with conn:
            conn.execute(
                "UPDATE mock_submissions SET status = 'interrupted', completed_at = ? WHERE submission_id = ? AND session_id = ?",
                (now_iso, submission_id, session_id),
            )
        return SubmissionAbortResponse(
            submission_id=submission_id,
            status="interrupted",
            message=f"Submission '{submission_id}' was aborted and will not count against submission limit.",
        )
    finally:
        conn.close()


@app.get("/submission/{submission_id}/status", response_model=SubmissionStatusResponse)
def get_submission_status(
    submission_id: str, authorization: str | None = Header(default=None)
) -> SubmissionStatusResponse:
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status, per_task_results FROM mock_submissions WHERE submission_id = ? AND session_id = ?",
            (submission_id, session_id),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(
                status_code=404,
                detail={"error": "SUBMISSION_NOT_FOUND", "message": f"Submission '{submission_id}' not found."},
            )
        cursor.execute("SELECT COUNT(*) FROM mock_tasks")
        total_tasks = cursor.fetchone()[0]
        results = json.loads(row["per_task_results"])
        return SubmissionStatusResponse(
            status=row["status"],
            tasks_completed=len(results),
            tasks_total=total_tasks,
            time_remaining_seconds=1800,
        )
    finally:
        conn.close()


@app.post("/submission/{submission_id}/finalize", response_model=SubmissionFinalizeResponse)
def finalize_submission(
    submission_id: str, authorization: str | None = Header(default=None)
) -> SubmissionFinalizeResponse:
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status FROM mock_submissions WHERE submission_id = ? AND session_id = ?",
            (submission_id, session_id),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(
                status_code=404,
                detail={"error": "SUBMISSION_NOT_FOUND", "message": f"Submission '{submission_id}' not found."},
            )
        now_iso = datetime.now(UTC).isoformat()
        with conn:
            conn.execute(
                "UPDATE mock_submissions SET status = 'completed', completed_at = ? WHERE submission_id = ?",
                (now_iso, submission_id),
            )
        return SubmissionFinalizeResponse(submission_id=submission_id, status="completed")
    finally:
        conn.close()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8001"))
    host = os.getenv("HOST", "127.0.0.1")
    print(f"Starting Agent Arena Mock Simulator on http://{host}:{port} (REVEAL_GROUND_TRUTH={REVEAL_GROUND_TRUTH})")
    uvicorn.run(app, host=host, port=port)
