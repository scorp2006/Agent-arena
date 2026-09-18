# Agent Arena: SupportOps — Participant Starter Kit

Welcome to **Agent Arena: SupportOps**! This repository is your complete toolkit for developing and benchmarking an autonomous customer support agent capable of resolving complex e-commerce, billing, and technical inquiries across realistic enterprise workflows.

---

## 1. 5-Minute Quickstart

Get up and running locally against the offline Mock Simulator in under 5 minutes:

### Step 1: Create and Activate Virtual Environment
```bash
# Create a fresh virtual environment
python -m venv .venv

# Activate on Windows (PowerShell):
.\.venv\Scripts\Activate.ps1
# Activate on Linux / macOS:
source .venv/bin/activate
```

### Step 2: Install All Dependencies
```bash
pip install -r requirements.txt
```
*(Installs both the participant runtime SDK and the local FastAPI/SQLite Mock Simulator).*

### Step 3: Configure Environment
```bash
cp .env.example .env
```
*(The default `.env` is preconfigured for offline local practice mode at `http://127.0.0.1:8001`).*

### Step 4: Launch Offline Mock Simulator (Terminal 1)
```bash
python mock_simulator/server.py --port 8001
```
Open your browser to the visual debugger dashboard:
👉 **`http://127.0.0.1:8001/dashboard`**

### Step 5: Test Your Agent in Practice Mode (Terminal 2)
```bash
# Run a single task with immediate ground-truth diff feedback:
python main.py --mode practice --once

# Or run multiple development tasks:
python main.py --mode practice --max-tasks 5
```

---

## 2. Architecture & File Responsibilities

The starter kit enforces a clean, modular boundary between the orchestration harness (`main.py`) and your AI agent (`agent.py`):

```text
┌─────────────────────────────────────────────────────────────┐
│                    main.py (Runtime Harness)                │
│  - Connects to Mock Simulator or Live Arena API             │
│  - Fetches assigned tasks & configures ToolsClient          │
│  - Handles LLM API key rotation & rate limit pacing         │
│  - Validates output contract schema & submits answers       │
└──────────────────────────────┬──────────────────────────────┘
                               │ passes (task, tools)
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                    agent.py (Your AI Agent)                 │
│  ★ THE ONLY FILE PARTICIPANTS EDIT                          │
│  - Analyzes customer message & inquiry type                 │
│  - Dispatches read tools to gather authoritative evidence   │
│  - Enforces policy compliance & takes server actions        │
│  - Returns structured Section 7 decision dictionary         │
└──────────────────────────────┬──────────────────────────────┘
                               │ returns Section 7 Dict
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                    Arena API / Mock Simulator               │
│  - Evaluates decision, policy actions & evidence grounding  │
│  - Scores submission across 7 macro dimensions              │
└─────────────────────────────────────────────────────────────┘
```

### Repository Structure:
| File / Directory | Purpose | Participant Action |
|:---|:---|:---|
| **`agent.py`** | Your core agent logic (`solve(task, tools, ...)`). | **Edit this file only** |
| **`main.py`** | Orchestration runtime, CLI flags, and submission engine. | Do not modify |
| **`sdk/tools_client.py`** | HTTP client exposing domain tools and Arena endpoints. | Read-only SDK |
| **`mock_simulator/`** | Offline server with 30 dev tasks and visual web debugger. | Local testing |
| **`sample_data/`** | Exported CSV datasets representing mock world state. | Reference / analysis |
| **`.env.example`** | Environment variable configuration template. | Copy to `.env` |
| **`requirements.txt`** | Unified dependencies for runtime and simulator. | `pip install -r` |

---

## 3. Task Input Contract

When `main.py` dispatches a task to `agent.solve(task, tools)`, the `task` dictionary contains:

```python
{
    "task_id": "TASK-DEV-0001",
    "customer_id": "CUS-50000001",
    "customer_message": "Hi there, I was looking through my online banking statement and noticed two identical charges of $99.00 on my Visa card from yesterday morning. Both charges seem to have gone through right around the same minute. Could you please check my account and refund the duplicate payment?"
}
```

### Task Attributes:
- **`task_id`** (`str`): Unique identifier for this support case. Pass this to `tools.escalate_case(case_id=task_id, ...)` when escalation is required.
- **`customer_id`** (`str`): Unique customer identifier used to query profiles, transaction histories, subscriptions, and previous tickets.
- **`customer_message`** (`str`): The raw inbound customer text spanning one of 6 inquiry categories (`billing`, `technical_support`, `account_access`, `policy_inquiry`, `order_status`, `returns_and_refunds`).

---

## 4. Tools Catalog (`tools: ToolsClient`)

The `tools` argument exposes **10 participant tools**. Each task has a budget of **100 tool calls** (server-enforced with HTTP 429 `BUDGET_EXCEEDED` if exceeded) and a rate limit of **6000 calls/min**. Budget counters automatically reset on every task.

### A. Read Tools (Investigation & Evidence Gathering)
Read tools inspect state without making permanent changes:

1. **`tools.search_knowledge(query: str, top_k: int = 5) -> dict[str, Any]`**
   - Searches policy manuals, refund windows, FAQs, and operating rules.
   - *Returns*: `{"results": [{"id": "DOC-1002", "title": "...", "snippet": "...", "category": "..."}]}`
2. **`tools.get_document(document_id: str) -> dict[str, Any]`**
   - Retrieves the full text, clauses, and effective date of a specific policy.
   - *Returns*: `{"document": {"id": "DOC-1002", "title": "...", "content": "...", "category": "..."}}`
3. **`tools.get_customer(customer_id: str) -> dict[str, Any]`**
   - Retrieves customer profile, tier status, identity verification status, and security challenges.
   - *Returns*: `{"customer": {"id": "...", "name": "...", "email": "...", "tier": "...", "verification_status": "..."}}`
4. **`tools.get_transactions(customer_id: str, start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]`**
   - Lists billing history, timestamps, payment methods, transaction amounts, and refund statuses.
   - *Returns*: `{"transactions": [{"id": "TXN-40101", "amount": 99.0, "status": "completed", ...}]}`
5. **`tools.get_subscription(customer_id: str) -> dict[str, Any]`**
   - Retrieves the active recurring subscription plan, renewal dates, and billing cycle.
   - *Returns*: `{"subscription": {"id": "SUB-40115", "plan_name": "...", "status": "active", ...}}`
6. **`tools.get_previous_cases(customer_id: str, limit: int = 5) -> dict[str, Any]`**
   - Retrieves past support tickets, prior resolutions, agent notes, and past concessions.
   - *Returns*: `{"cases": [{"case_id": "CASE-40126", "date": "...", "resolution": "...", "notes": "..."}]}`

### B. Action Tools (Server-Enforced State Mutations)
Action tools make modifications and are validated against authoritative business rules:

7. **`tools.issue_refund(transaction_id: str, amount: float, reason: str) -> dict[str, Any]`**
   - Issues a full or partial refund. Enforces policy refund windows (e.g. 30 days per `DOC-1001`), dispute lock statuses, and daily maximums.
8. **`tools.cancel_subscription(customer_id: str, subscription_id: str) -> dict[str, Any]`**
   - Cancels recurring billing. Verifies contractual minimum commitments and checks for open billing disputes per `DOC-1003`.
9. **`tools.escalate_case(case_id: str, team: str, reason: str) -> dict[str, Any]`**
   - Routes case to specialized teams (`billing_specialists`, `logistics_investigations`, `security_operations`, `retention_specialists`). Reason must cite concrete evidence.
10. **`tools.request_verification(customer_id: str, verification_type: str = "identity") -> dict[str, Any]`**
    - Triggers MFA / ID verification challenge for unverified accounts or suspicious security events.

---

## 5. Output Contract (Section 7)

Your `agent.solve(task, tools)` function must return a structured dictionary conforming to the standard Section 7 contract:

```python
{
    "case_classification": {
        "category": "billing",          # "billing" | "technical_support" | "account_access" | "policy_inquiry" | "order_status" | "returns_and_refunds"
        "issue": "duplicate_charge",    # Concise issue identifier
        "severity": "medium"            # "low" | "medium" | "high" | "critical"
    },
    "decision": {
        "resolution": "refund",         # "refund" | "cancel_subscription" | "escalate" | "deny" | "answer_only" | "request_verification"
        "escalation_required": False    # bool: True if escalated to specialized human team
    },
    "evidence": [                       # List of entity/document IDs observed during tool calls
        "DOC-1002",
        "TXN-40101",
        "TXN-40102"
    ],
    "uncertainties": [],                # Any remaining ambiguity or information gaps
    "customer_response": (              # Polite, empathetic, and professional customer-facing explanation
        "We have verified the duplicate charge of $99.00 and issued a full refund to your card per DOC-1002."
    ),
    "confidence": 0.95                  # Calibrated confidence score (float: 0.0 to 1.0)
}
```

---

## 6. Offline Development Dataset (`sample_data/`)

The `sample_data/` directory contains pre-dumped CSV files representing the mock world state (Seed 1000):
- **`tasks.csv`**: The 30 development tasks with complete **ground truth reference answers**.
- **`customers.csv`**: Customer demographic and account verification data.
- **`transactions.csv`**: Past transactions, amounts, and statuses.
- **`subscriptions.csv`**: Recurring subscription plans and renewal cycles.
- **`policies.csv`**: Authoritative refund, return, security, and cancellation policies.
- **`previous_cases.csv`**: Historical support ticket history.

> **CRITICAL COMPETITION NOTE:**
> - The mock dataset (Seed 1000) and the live competition dataset (Seed 50000+) are **completely disjoint**.
> - In live competition evaluation, customer names, transaction IDs, and policy details will be unseen.
> - **Do NOT hardcode answers or regex rules based on CSV files.** Your agent must dynamically query the `tools` client.

---

## 7. Execution Modes

### Mode A: Practice Mode (Local Iteration)
Ideal for developing, debugging, and benchmarking locally:
```bash
# Process a single task and exit with diff analysis:
python main.py --mode practice --once

# Process first N tasks:
python main.py --mode practice --max-tasks 5

# Process all 30 development tasks:
python main.py --mode practice
```
Check `http://127.0.0.1:8001/dashboard` for live task-by-task visual score reports.

### Mode B: Submission Mode (Live Arena Platform)
When you are ready to compete on the official Arena platform:
1. In `.env`, set:
   ```env
   MODE=submission
   SUBMISSION_ARENA_URL=https://wiring-repeater-untitled.ngrok-free.dev
   SUBMISSION_BEARER_TOKEN=your-bearer-token-assigned-at-registration
   ```
2. Run official submission:
   ```bash
   python main.py --mode submission
   ```
In submission mode:
- All 30 competition tasks are fetched upfront in randomized order.
- Your agent executes tasks sequentially in memory (rate-limit safe).
- Solutions are submitted in a single atomic batch (`POST /submission/{id}/submit_batch`).
- Your verified score card is rendered upon completion.

---

## 8. Macro Scoring Dimensions

Submissions are evaluated across 7 orthogonal dimensions (0% – 100%):

1. **Task Success (35% weight)**: Correctness of resolution decision and escalation flag matching ground truth.
2. **Policy Adherence (15% weight)**: Server-enforced eligibility validation (zero illegal refunds or cancellations).
3. **Evidence Grounding (15% weight)**: Precision and recall of cited evidence IDs (`DOC-*`, `TXN-*`, `CUS-*`).
4. **Calibration (10% weight)**: Accuracy of confidence estimates (Brier score alignment).
5. **Efficiency (10% weight)**: Operating within the 100 tool-call budget without redundant calls.
6. **Communication (10% weight)**: Professionalism, empathy, and clarity of `customer_response`.
7. **Robustness (5% weight)**: Resilience against edge cases, missing parameters, and conflicting requests.

---

## 9. Pro-Tips for Winning

- **Automatic Key Rotation**: Configure `GOOGLE_API_KEY_1` through `GOOGLE_API_KEY_5` in `.env`. `main.py` rotates keys round-robin across tasks to avoid 429 quota exhaustion.
- **Cite Only Observed Evidence**: Only include IDs in `evidence: [...]` that actually returned from tool calls. Hallucinated IDs trigger precision penalties.
- **Check Previous Precedent**: Query `tools.get_previous_cases()` when customer claims an exception or prior promise.
- **Always Validate Policy First**: Call `tools.search_knowledge()` before issuing refunds or cancellations.

