# Sample Mock Data & Ground Truth Reference Answers

This folder contains a clean tabular export of the mock environment world state and the 30 development tasks **with all corresponding ground truth answers**.

---

## Files Included

1. **`tasks.csv`**
   - The 30 development task inquiries evaluated by the mock simulator.
   - Columns:
     - `task_id`: Unique identifier of the task (e.g. `TASK-DEV-0001`).
     - `customer_id`: Associated customer identifier.
     - `customer_message`: Inbound customer inquiry message.

2. **`ground_truth.csv`**
   - Corresponding ground truth reference answers matching `task_id`.
   - Columns:
     - `task_id`: Matches task identifier from `tasks.csv`.
     - `expected_resolution`: Ground truth resolution (`refund`, `deny`, `escalate`, `request_info`).
     - `must_escalate`: Whether human supervisor escalation is required (`True` / `False`).
     - `required_evidence`: JSON array of required evidence IDs that must be cited.
     - `category`: Ground truth case category (e.g. `billing`, `shipping`, `fraud`).
     - `issue`: Specific classified issue.
     - `severity`: Assessed severity (`low`, `medium`, `high`, `critical`).
     - `expected_action`: Expected state-mutating tool action, if applicable.

3. **`customers.csv`**
   - Customer profile records.
   - Columns: `id`, `name`, `email`, `tier`, `region`, `verification_status`, `account_status`, `created_at`.
   - Access at runtime via: `tools.get_customer(customer_id)`.

4. **`subscriptions.csv`**
   - Customer recurring subscription records and cancellation eligibility constraints.
   - Columns: `id`, `customer_id`, `plan`, `billing_cycle`, `amount`, `status`, `start_date`, `renewal_date`, `lock_in_until`, `has_approved_exception`, `has_unresolved_dispute`.
   - Access at runtime via: `tools.get_subscription(customer_id)`.

5. **`transactions.csv`**
   - Customer financial transactions, including refund and dispute statuses.
   - Columns: `id`, `customer_id`, `amount`, `currency`, `date`, `status`, `description`, `invoice_id`, `chargeback_status`, `under_fraud_investigation`, `refund_status`, `refunded_amount`, `payment_method`.
   - Access at runtime via: `tools.get_transactions(customer_id)`.

6. **`policies.csv`**
   - SupportOps operating procedures and policies.
   - Columns: `id`, `title`, `category`, `updated_at`, `content`.
   - Access at runtime via: `tools.get_document(document_id)` or `tools.search_knowledge(query)`.

7. **`previous_cases.csv`**
   - Historical support tickets and resolutions (crucial for `previous_agent_was_wrong` investigations).
   - Columns: `case_id`, `customer_id`, `date`, `category`, `resolution`, `agent_id`, `notes`, `evidence_used`.
   - Access at runtime via: `tools.get_previous_cases(customer_id)`.
