# Agent Implementation Guide — SupportOps

> **File to Edit:** `agent.py`  
> **Goal:** Build an autonomous customer support agent that investigates tickets, enforces business policies, takes server-validated actions, and returns structured resolutions.

---

## 1. Quick Start

In this challenge, **`agent.py` is the only file you modify**. The orchestration harness (`main.py`) calls your `solve()` function for every task.

```python
def solve(
    task: dict[str, Any],
    tools: ToolsClient,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    ...
```

---

## 2. What Your Agent Receives (`task`)

Each `task` dictionary contains:

| Field | Type | Description | Example |
|-------|------|-------------|---------|
| `task_id` | `str` | Unique ID for the current task | `"TASK-001"` |
| `customer_id` | `str` | Customer requesting assistance | `"CUS-1001"` |
| `customer_message` | `str` | Customer's raw message or complaint | `"I was charged twice for order TXN-501..."` |

---

## 3. Available Tools (`tools`)

Your `tools` object provides **10 methods** (6 read tools, 4 action tools):

### Read Tools (Information Gathering)
Call these to look up facts before making a decision. They do not modify world state:

```python
# Search policy knowledge base
docs = tools.search_knowledge(query="refund policy duplicate charge", top_k=5)

# Fetch full text of a specific policy document
doc = tools.get_document(document_id="DOC-1001")

# Look up customer profile (tier, account status)
customer = tools.get_customer(customer_id="CUS-1001")

# Fetch customer transaction history
txns = tools.get_transactions(customer_id="CUS-1001", start_date=None, end_date=None)

# Look up active subscription details
sub = tools.get_subscription(customer_id="CUS-1001")

# View past support tickets and resolutions
cases = tools.get_previous_cases(customer_id="CUS-1001", limit=5)
```

### Action Tools (Server-Side Enforced State Mutations)
Call these when an action is warranted. The server strictly validates eligibility:

```python
# Issue full or partial refund (checks: amount <= total, within window, no active chargeback)
tools.issue_refund(transaction_id="TXN-501", amount=99.0, reason="duplicate_charge")

# Cancel active subscription (checks: not in lock-in period unless exception, no disputes)
tools.cancel_subscription(customer_id="CUS-1001", subscription_id="SUB-201")

# Escalate to human specialist team (reason MUST cite at least one retrieved evidence ID)
tools.escalate_case(case_id="CASE-301", team="billing_specialists", reason="Active dispute on TXN-501 per DOC-1842")

# Request identity or security verification (safe fallback — always succeeds)
tools.request_verification(customer_id="CUS-1001", verification_type="identity")
```

---

## 4. What Your Agent Must Return

Your `solve()` function must return a structured dictionary conforming to this contract:

```python
return {
    "task_id": task["task_id"],           # Required: match task_id
    "case_classification": {
        "category": "refund_request",     # "refund_request" | "subscription_cancellation" | "account_lock_fraud" | "billing_dispute" | "general_inquiry" | "adversarial"
        "issue": "duplicate_charge",      # Short description of the issue
        "severity": "medium",             # "low" | "medium" | "high" | "critical"
    },
    "decision": {
        "resolution": "refund",           # "refund" | "deny" | "escalate" | "request_info"
        "escalation_required": False,     # True if escalate_case was called, False otherwise
    },
    "evidence": [                         # List of entity/document IDs retrieved via tools
        "TXN-501",
        "DOC-1001"
    ],
    "uncertainties": [],                  # Optional: list of unresolved doubts
    "customer_response": (                # 20–5,000 characters: polite, grounded reply to customer
        "Hello, we have verified the duplicate charge for transaction TXN-501 "
        "and issued a refund of $99.00 back to your original payment method."
    ),
    "confidence": 0.95,                   # Float between 0.0 and 1.0 reflecting certainty
}
```

---

## 5. Recommended Implementation Pattern

A clean, reliable agent structure follows 5 steps:

```python
def solve(task, tools, api_key=None, model=None, base_url=None):
    task_id = task.get("task_id", "")
    customer_id = task.get("customer_id", "")
    msg = task.get("customer_message", "")
    evidence = []

    # Step 1: Gather facts
    customer = tools.get_customer(customer_id)
    evidence.append(customer_id)

    # Step 2: Retrieve relevant records based on message intent
    txns = tools.get_transactions(customer_id)
    # Collect transaction IDs observed...

    policies = tools.search_knowledge(msg)
    # Collect policy document IDs observed...

    # Step 3: Evaluate eligibility against authoritative policies
    # - Check refund window (default 30 days)
    # - Check active chargeback status (DOC-1842 blocks refund)
    # - Check subscription lock-in period

    # Step 4: Execute action tool if eligible
    # e.g., tools.issue_refund(...) or tools.cancel_subscription(...)

    # Step 5: Construct and return final response
    return {
        "task_id": task_id,
        "case_classification": {...},
        "decision": {...},
        "evidence": evidence,
        "uncertainties": [],
        "customer_response": "...",
        "confidence": 0.9,
    }
```

---

## 6. Golden Rules for Maximum Score

1. **Stay Under Budget:** You have **40 tool calls per task**. Efficiency drops if you call too many tools or make repeated duplicate calls.
2. **Never Fabricate Evidence:** Only cite IDs (`TXN-xxx`, `DOC-xxx`, `CUS-xxx`, `SUB-xxx`) that were **actually returned** by tool responses. Fabricated IDs heavily penalize your Evidence Grounding score.
3. **Calibrate Confidence:** If unsure or missing info, lower your confidence (e.g. `0.3`–`0.5`). Overconfident errors are penalized.
4. **Be Consistent in Customer Replies:** Do not promise a refund in `customer_response` if you submitted `resolution: "deny"` or if the refund was rejected.
5. **Ground Escalations:** If calling `escalate_case()`, the reason **must** cite at least one real evidence ID (e.g., `"Per DOC-1842, TXN-001 has active dispute"`).

---

## 7. How to Test Your Agent

```bash
# 1. Start the mock simulator in one terminal:
python mock_simulator/server.py

# 2. Run your agent in another terminal:
python main.py
```

Inspect live results on the mock dashboard: `http://127.0.0.1:8001/dashboard`.
