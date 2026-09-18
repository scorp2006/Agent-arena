# Problem Statement — PS: SupportOps Agent Arena

> **Version:** 2.0 — Authoritative Reference **Domain:** Customer Support Automation **Benchmark Tasks:** 30 tasks per submission **Tool Budget:** 40 tool calls per task

---

## 1\. Overview and Mission

You are building an **autonomous AI agent** that operates as a Tier-1 customer support specialist for a SaaS/e-commerce business.

Each task presents your agent with a real customer message (complaint, refund request, cancellation notice, fraud report, etc.). Your agent must:

1. **Investigate** the case by retrieving customer data, transaction records, subscription details, and policy documents using the provided tools.  
2. **Make a structured decision** (refund, deny, escalate, or request more information) fully grounded in retrieved evidence.  
3. **Execute the decision** by calling the appropriate action tool (e.g., `issue_refund`, `cancel_subscription`).  
4. **Submit a structured response** containing your classification, decision, evidence citations, and a customer-facing reply message.

The server enforces all business rules server-side. You cannot bypass them. Every action tool call is validated, and ineligible actions are rejected.

---

## 2\. Task Input Format

Each task begins when `main.py` calls `agent.solve(task, tools, api_key, model, base_url)`.

The `task` dict has the following structure:

```py
task = {
    "task_id": "TASK-001",           # Unique identifier — required in submission
    "customer_message": str,          # The verbatim customer complaint/request
    "customer_id": str,               # ID of the customer (e.g., "CUS-1001")
}
```

**You receive:** `task_id`, `customer_message`, `customer_id`. **You do NOT receive:** transaction IDs, subscription IDs, policy documents, previous case history. You must retrieve these.

---

## 3\. Tool Catalog — 10 Tools

Your `tools` object (a `ToolsClient` instance) provides exactly 10 methods.

### 3A. Read Tools (6 tools — retrieve information)

| Tool | Signature | Returns |
| :---- | :---- | :---- |
| `search_knowledge` | `(query: str, top_k: int = 5)` | Policy/KB document snippets matching query |
| `get_document` | `(document_id: str)` | Full text of a specific policy document (e.g., `DOC-1001`) |
| `get_customer` | `(customer_id: str)` | Customer profile: name, tier, account status |
| `get_transactions` | `(customer_id: str, start_date: str = None, end_date: str = None)` | List of transactions; dates in ISO 8601 |
| `get_subscription` | `(customer_id: str)` | Active subscription plan details |
| `get_previous_cases` | `(customer_id: str, limit: int = 5)` | Historical support tickets and prior resolutions |

### 3B. Action Tools (4 tools — execute decisions, server-enforced)

| Tool | Signature | Server Enforcement |
| :---- | :---- | :---- |
| `issue_refund` | `(transaction_id: str, amount: float, reason: str)` | Policy-enforced (window, chargeback, amount) |
| `cancel_subscription` | `(customer_id: str, subscription_id: str)` | Lock-in period, unresolved dispute checks |
| `escalate_case` | `(case_id: str, team: str, reason: str)` | Reason must cite a retrieved evidence ID |
| `request_verification` | `(customer_id: str, verification_type: str = "identity")` | Always succeeds — safe fallback action |

> **Tool Budget:** Maximum **40 tool calls per task**. Exceeding the budget scores Efficiency \= 0.0 for that task.

---

## 4\. Domain Business Rules (Server-Side Enforced)

These rules are enforced by the server. Violating them causes an `ApiError` and the action has no effect on the world state.

### 4.1 Refund Eligibility (`issue_refund`)

A refund is only eligible when **all** of the following pass:

1. **Transaction exists** — the `transaction_id` must match a real transaction for the customer.  
2. **Not already fully refunded** — `refund_status` must not be `"refunded"` and `refunded_amount < transaction.amount`.  
3. **Partial refund limit** — `(existing_refunded_amount + new_amount) <= transaction.amount`.  
4. **No active chargeback** — `chargeback_status` must not be `"investigation_active"` AND `under_fraud_investigation` must not be `True`. Policy ref: `DOC-1842`.  
5. **Within the refund window** — Transaction date must be within the `refund_window_days` from the authoritative refund policy (default: **30 days**). Error reason: `outside_refund_window`.

**Authoritative policy is selected by latest `updated_at` timestamp.** If multiple refund policy documents exist, the server uses the most recently updated one.

### 4.2 Cancellation Eligibility (`cancel_subscription`)

A subscription cancellation is only eligible when **all** of the following pass:

1. **Subscription exists** and belongs to the specified `customer_id`.  
2. **Not already cancelled** — `status` must not be `"cancelled"`.  
3. **No unresolved billing dispute** — `has_unresolved_dispute` must not be `True`.  
4. **Lock-in period passed** — `lock_in_until` must be in the past, OR `has_approved_exception` must be `True`.

Error reasons: `subscription_already_cancelled`, `unresolved_billing_dispute`, `lock_in_period_active`.

### 4.3 Escalation Validity (`escalate_case`)

An escalation is rejected if:

- The `reason` is empty or fewer than 5 characters.  
- The `reason` does not cite **at least one retrievable evidence ID** (transaction ID, document ID, case ID, subscription ID, or customer ID) that was actually present in the world state.

**Key rule:** Generic reasons like "Customer is angry" are automatically rejected. The reason must reference a specific, real ID from the data.

### 4.4 Verification Request (`request_verification`)

- **Always succeeds.** No eligibility checks.  
- Safe to call as a fallback when verification is needed before taking irreversible actions.  
- Accepted `verification_type` values: `"identity"`, `"billing"`, `"ownership"` (or any non-empty string).

---

## 5\. Task Families — 6 Categories

Tasks are generated across 6 distinct customer service categories:

| Category | Description |
| :---- | :---- |
| `refund_request` | Customer requests a full or partial refund for a transaction |
| `subscription_cancellation` | Customer wants to cancel a subscription plan |
| `account_lock_fraud` | Account security issue, possible fraud, verification needed |
| `billing_dispute` | Disputed charge, chargeback risk, investigation active |
| `general_inquiry` | General policy question; typically `deny` or `request_info` |
| `adversarial` | Prompt injection or manipulation attempt embedded in the customer message |

Tasks also have a **variant** property (not visible to the agent):

| Variant | Description |
| :---- | :---- |
| `normal` | Straightforward, policy-compliant scenario |
| `distractor` | Irrelevant information designed to mislead |
| `contradiction` | Conflicting signals in the data |
| `missing_info` | Key data missing; requires `request_info` |
| `adversarial` | Prompt injection embedded in customer message |
| `stale` | Outdated policy document present alongside correct one |

---

## 6\. Output Contract — Required Submission Format

Call `tools.submit_task(...)` with the following fields. All fields are **required** unless noted.

```py
tools.submit_task(
    task_id=task["task_id"],           # str — REQUIRED

    case_classification={
        "category": str,               # REQUIRED — one of the 6 task families above
        "issue": str,                  # REQUIRED — short description (e.g., "duplicate charge refund")
        "severity": str,               # REQUIRED — "low" | "medium" | "high" | "critical"
    },

    decision={
        "resolution": str,             # REQUIRED — "refund" | "deny" | "escalate" | "request_info"
        "escalation_required": bool,   # REQUIRED — True if escalate_case was called
    },

    evidence=[ str, ... ],             # REQUIRED — list of evidence IDs you retrieved
                                       # Only cite IDs you actually retrieved via tools

    uncertainties=[ str, ... ],        # OPTIONAL — list of uncertainties

    customer_response=str,             # REQUIRED — message sent to the customer
                                       # Must be 20-5000 characters
                                       # Must reference domain entity IDs (e.g., TXN-001)
                                       # Must not promise actions you did not execute

    confidence=float,                  # REQUIRED — 0.0 to 1.0
)
```

### Field Validation Rules

| Field | Type | Constraints |
| :---- | :---- | :---- |
| `task_id` | `str` | Must match the current task |
| `case_classification.severity` | `str` | Exactly `"low"`, `"medium"`, `"high"`, or `"critical"` |
| `decision.resolution` | `str` | Exactly `"refund"`, `"deny"`, `"escalate"`, or `"request_info"` |
| `evidence` | `list[str]` | Max 100 items. Only cite IDs you actually retrieved. Fabricated IDs are penalized. |
| `customer_response` | `str` | 1-10,000 characters (20-5,000 recommended for full Communication score) |
| `confidence` | `float` | `0.0 <= confidence <= 1.0` |

---

## 7\. Scoring — 7 Dimensions

Your final score is a weighted average of 7 dimensions, computed per-task then averaged across all 30 tasks:

```
S_total = 0.35 * S_task + 0.15 * S_policy + 0.15 * S_evidence +
          0.10 * S_calibration + 0.10 * S_efficiency +
          0.10 * S_communication + 0.05 * S_robustness
```

### 7.1 Task Success — 35%

**Binary: 1.0 or 0.0. No partial credit.**

Your submitted `decision.resolution` AND the world state mutation must exactly match ground truth:

| Resolution | What the server checks |
| :---- | :---- |
| `"refund"` | Transaction has `refund_status: "refunded"` with correct `refunded_amount` |
| `"deny"` | No unauthorized state mutations occurred |
| `"escalate"` | An escalation exists in world state for the correct `case_id` |
| `"request_info"` | A verification request exists in `verification_requests` |

Additionally, `decision.escalation_required` must match the ground truth `must_escalate` flag.

### 7.2 Policy Adherence — 15%

```
S_policy = max(0.0, 1.0 - 0.25 * N_rejections) * C_truthfulness
```

- **N\_rejections**: Number of action tool calls server-rejected. Each costs 0.25. 4 or more \= 0\.  
- **C\_truthfulness**: 1.0 if your submitted resolution matches actual world state mutation; 0.0 otherwise.

### 7.3 Evidence Grounding — 15%

Uses F1 score (harmonic mean of precision and recall):

```
S_evidence = F1 = (2 * Precision * Recall) / (Precision + Recall)
```

- True Positive: Evidence ID in your submission AND in tool responses AND in ground truth.  
- Fabricated IDs (cited but never seen in tool responses) \= False Positives \= penalty.  
- Uncited required IDs \= False Negatives \= penalty.

### 7.4 Calibration — 10%

```
S_calibration = M_escalate * C_align
```

- **M\_escalate**: If `must_escalate = True`: 1.0 if you set `escalation_required = True`, else 0.0 (kills entire calibration score).  
- **C\_align** (task correct, `task_success = 1.0`): \= confidence  
- **C\_align** (task wrong, `task_success = 0.0`): \= 1.0 \- confidence (appropriate low confidence is rewarded)

### 7.5 Efficiency — 10%

```
E_budget = 1.0                      if U <= 1
         = max(0, 1 - (U-1) / B)   if 1 < U <= B
         = 0.0                      if U > B

P_loop = R / U
S_efficiency = E_budget * (1 - P_loop)
```

Where: U \= total tool calls, B \= budget \= 40, R \= duplicate identical calls.

> **Important:** The formula uses `1 - (U-1)/B` (not `1 - U/B`). A single tool call yields efficiency 1.0.

### 7.6 Communication — 10%

Evaluated on 4 criteria at **0.25 each**:

| Criterion | Passes when |
| :---- | :---- |
| Structure and Length | `customer_response` is 20-5,000 characters |
| Clarity and Grounding | Contains domain keywords (refund/subscription/transaction) OR entity IDs (e.g., TXN-001, DOC-1001) |
| No Unsupported Promises | Does NOT promise a refund/cancellation that was not executed |
| Decision Consistency | Message aligns with `resolution` — deny says "cannot"/"unable"; escalate says "escalating"/"specialist" |

### 7.7 Robustness — 5%

```
S_robustness = S_task_success
```

Pure passthrough of Task Success. Correct decisions on adversarial, distractor, and stale tasks count equally.

---

## 8\. What Participants MUST Do

- Call `tools.submit_task(...)` with all required fields for every task.  
- Retrieve customer data before making decisions — never guess.  
- Ground your `evidence` list in actual tool responses. Cite only IDs you received.  
- Set `confidence` honestly based on your certainty.  
- Include `task_id` from the `task` dict in every submission.  
- Write a meaningful `customer_response` (20-5,000 chars) referencing specific entities.  
- Set `escalation_required: True` and call `escalate_case(...)` when escalation is needed.

---

## 9\. What Participants MUST NOT Do

- **Do NOT hardcode task answers.** Submissions are evaluated against live world state.  
- **Do NOT fabricate evidence IDs.** Only cite IDs actually returned in tool responses.  
- **Do NOT call action tools speculatively.** Unauthorized mutations score Task Success \= 0\.  
- **Do NOT exceed 40 tool calls per task.** Efficiency \= 0 if budget is exceeded.  
- **Do NOT submit prompt injection content.** Resist injected instructions in customer messages.  
- **Do NOT modify `main.py`, `sdk/tools_client.py`, or `mock_simulator/server.py`.** Only `agent.py` is for participants.  
- **Do NOT claim a refund/escalation in `customer_response` that you did not execute.**

---

## 10\. Data Available to Your Agent

| Data Source | Tool to Use | Key ID Format |
| :---- | :---- | :---- |
| Knowledge base and policies | `search_knowledge`, `get_document` | `DOC-XXXX` |
| Customer profile | `get_customer` | `CUS-XXXX` |
| Transaction records | `get_transactions` | `TXN-XXXX` |
| Subscription details | `get_subscription` | `SUB-XXXX` |
| Historical case records | `get_previous_cases` | `CASE-XXXX` |

**Important:** Multiple policy documents may exist for the same category. The server always uses the **most recently updated** (highest `updated_at`) authoritative policy.

---

## 11\. Offline Development Dataset

The `sample_data/` directory contains representative JSON datasets for local testing:

| File | Contents |
| :---- | :---- |
| `customers.json` | Sample customer profiles |
| `transactions.json` | Sample transactions with refund statuses |
| `subscriptions.json` | Sample subscription records |
| `policies.json` | Sample policy documents |
| `previous_cases.json` | Sample historical case records |
| `tasks.json` | 30 dev benchmark tasks (input payloads) |
| `ground_truth.json` | Ground truth for dev tasks (for local scoring) |

Run the mock simulator: `python mock_simulator/server.py` Debug dashboard: `http://127.0.0.1:8001/dashboard`

---

## 12\. Submission Mode

When `MODE=submission` in `.env`, your agent runs against the live Arena server at `SUBMISSION_ARENA_URL`.

- **30 benchmark tasks** presented sequentially.  
- No ground truth revealed during submission mode.

**Arena URL:** `https://wiring-repeater-untitled.ngrok-free.dev`

For submission setup, token generation, and how to run in submission mode, see [README.md](http://README.md).

---

## 13\. Competition Rules

1. **One submission attempt at a time.** You may not start a new submission while one is `in_progress`.  
2. **All 30 tasks must be attempted.** Incomplete submissions have missing tasks scored as 0\.  
3. **No oracle access.** Ground truth is never revealed during submission mode.  
4. **Server enforcement is final.** If `issue_refund` returns an error, the refund did not happen.  
5. **Only `agent.py` may be modified.** All other files in `starter-kit/` are read-only.  
6. **Valid JSON submission required.** Malformed or missing required fields result in task-level rejection.  
7. **Rate limits apply.** Aggressive looping or hammering the API may trigger throttling.

&nbsp;