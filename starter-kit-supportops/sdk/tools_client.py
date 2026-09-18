"""Agent Arena SupportOps — Official Python SDK.

Provides two clean client interfaces:
1. ToolsClient: Participant-facing client passed to agent.solve(task, tools).
   Contains ONLY the 10 SupportOps read and action tools.
2. ArenaClient: Orchestration client used by main.py.
   Handles connection, auth, task retrieval, submission, and lifecycle.
"""

import os
import time
from typing import Any

import httpx


class TransportError(Exception):
    """Raised when an HTTP transport or network-level error occurs."""

    def __init__(self, message: str, original_exception: Exception | None = None):
        super().__init__(message)
        self.original_exception = original_exception


class ApiError(Exception):
    """Raised when the API returns an HTTP 4xx or 5xx error."""

    def __init__(self, status_code: int, detail: Any):
        super().__init__(f"API error {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class ToolsClient:
    """Participant-facing tools client passed into agent.solve(task, tools).

    Exposes ONLY the 10 SupportOps read and action tools.
    The agent does NOT need to manage tasks, polling, or submissions.
    """

    def __init__(
        self,
        base_url: str | httpx.Client | None = None,
        token: str | None = None,
        timeout: float = 30.0,
        on_tool_call: Any | None = None,
        client: httpx.Client | None = None,
    ):
        if client is not None:
            self._client = client
            self._owns_client = False
            self.base_url = str(client.base_url).rstrip("/")
            self.token = token or ""
        elif isinstance(base_url, httpx.Client):
            self._client = base_url
            self._owns_client = False
            self.base_url = str(base_url.base_url).rstrip("/")
            self.token = token or ""
        else:
            raw_url = base_url or os.getenv("BASE_URL") or "http://localhost:8000"
            self.base_url = raw_url.rstrip("/")
            self.token = token or os.getenv("BEARER_TOKEN") or "dev-practice-token"
            self._client = httpx.Client(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                    "ngrok-skip-browser-warning": "true",
                },
                timeout=timeout,
            )
            self._owns_client = True
        self._on_tool_call = on_tool_call
        self._active_task_id: str | None = None

    def set_active_task(self, task_id: str) -> None:
        """Sets the active task ID so subsequent tool calls are scoped to this task."""
        self._active_task_id = task_id

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "ToolsClient":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def _post(self, path: str, json_data: dict[str, Any] | None = None) -> dict[str, Any]:
        t0 = time.time()
        tool_name = path.replace("/tools/", "")
        kwargs: dict[str, Any] = {"json": json_data if json_data is not None else {}}
        if self._active_task_id:
            kwargs["headers"] = {"X-Task-ID": self._active_task_id}
        try:
            resp = self._client.post(path, **kwargs)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
            if self._on_tool_call:
                self._on_tool_call(tool_name, json_data, None, 0, is_error=True)
            raise TransportError(f"Network transport error calling {path}: {e}", original_exception=e) from e

        latency_ms = int((time.time() - t0) * 1000)

        if resp.status_code >= 400:
            try:
                err_detail = resp.json()
            except Exception:
                err_detail = resp.text
            if self._on_tool_call:
                self._on_tool_call(tool_name, json_data, err_detail, latency_ms, is_error=True)
            raise ApiError(resp.status_code, err_detail)

        try:
            data = resp.json()
            if self._on_tool_call:
                self._on_tool_call(tool_name, json_data, data, latency_ms, is_error=False)
            return data
        except Exception as e:
            raise TransportError(f"Malformed JSON response from {path}: {resp.text}", original_exception=e) from e

    # =========================================================================
    # Read Tools (6 endpoints)
    # =========================================================================

    def search_knowledge(self, query: str, top_k: int = 5) -> dict[str, Any]:
        """Searches policies and knowledge base documents for relevant snippets."""
        return self._post("/tools/search_knowledge", {"query": query, "top_k": top_k})

    def get_document(self, document_id: str) -> dict[str, Any]:
        """Retrieves full text and metadata of a specific policy or document."""
        return self._post("/tools/get_document", {"document_id": document_id})

    def get_customer(self, customer_id: str) -> dict[str, Any]:
        """Retrieves customer profile and account status."""
        return self._post("/tools/get_customer", {"customer_id": customer_id})

    def get_transactions(
        self,
        customer_id: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """Retrieves customer transaction history with optional ISO date range filters."""
        payload: dict[str, Any] = {"customer_id": customer_id}
        if start_date:
            payload["start_date"] = start_date
        if end_date:
            payload["end_date"] = end_date
        return self._post("/tools/get_transactions", payload)

    def get_subscription(self, customer_id: str) -> dict[str, Any]:
        """Retrieves customer active subscription details if any."""
        return self._post("/tools/get_subscription", {"customer_id": customer_id})

    def get_previous_cases(self, customer_id: str, limit: int = 5) -> dict[str, Any]:
        """Retrieves historical support tickets and previous resolutions for the customer."""
        return self._post("/tools/get_previous_cases", {"customer_id": customer_id, "limit": limit})

    # =========================================================================
    # Action Tools (4 endpoints, server-side enforced)
    # =========================================================================

    def issue_refund(self, transaction_id: str, amount: float, reason: str) -> dict[str, Any]:
        """Attempts to refund a transaction. Server-side policy enforcement applies."""
        return self._post(
            "/tools/issue_refund",
            {
                "transaction_id": transaction_id,
                "amount": amount,
                "reason": reason,
            },
        )

    def cancel_subscription(self, customer_id: str, subscription_id: str) -> dict[str, Any]:
        """Attempts to cancel a customer subscription. Lock-in and dispute checks apply."""
        return self._post(
            "/tools/cancel_subscription",
            {
                "customer_id": customer_id,
                "subscription_id": subscription_id,
            },
        )

    def escalate_case(self, case_id: str, team: str, reason: str) -> dict[str, Any]:
        """Escalates case to a specialized team. Reason must cite retrievable evidence."""
        return self._post(
            "/tools/escalate_case",
            {
                "case_id": case_id,
                "team": team,
                "reason": reason,
            },
        )

    def request_verification(self, customer_id: str, verification_type: str = "identity") -> dict[str, Any]:
        """Requests secondary identity or billing verification from the customer (safe fallback)."""
        return self._post(
            "/tools/request_verification",
            {
                "customer_id": customer_id,
                "verification_type": verification_type,
            },
        )

    def _get(self, path: str) -> dict[str, Any]:
        try:
            resp = self._client.get(path)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
            raise TransportError(f"Network transport error calling {path}: {e}", original_exception=e) from e

        if resp.status_code >= 400:
            try:
                err_detail = resp.json()
            except Exception:
                err_detail = resp.text
            raise ApiError(resp.status_code, err_detail)

        try:
            return resp.json()
        except Exception as e:
            raise TransportError(f"Malformed JSON response from {path}: {resp.text}", original_exception=e) from e

    # --- Task and Submission Flow (orchestration / compatibility methods) ---

    def get_task(self) -> dict[str, Any]:
        """Requests assignment of the next task in the active submission or practice pool."""
        return self._post("/task/start")

    # Backwards compatibility alias
    start_task = get_task

    def submit_task(
        self,
        task_id: str,
        case_classification: dict[str, Any] | None = None,
        decision: dict[str, Any] | None = None,
        evidence: list[str] | None = None,
        uncertainties: list[str] | None = None,
        customer_response: str | None = None,
        confidence: float | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Submits structured decision, evidence citations, and response for grading."""
        if payload is not None:
            body = dict(payload)
        else:
            body = {
                "case_classification": case_classification,
                "decision": decision,
                "evidence": evidence,
                "uncertainties": uncertainties,
                "customer_response": customer_response,
                "confidence": confidence,
            }
        body["task_id"] = task_id
        return self._post("/task/submit", body)

    # --- Submission Lifecycle ---

    def start_submission(self) -> dict[str, Any]:
        """Starts a full evaluation submission run."""
        return self._post("/submission/start")

    def get_submission_status(self, submission_id: str) -> dict[str, Any]:
        """Fetches progress and time remaining for a submission run."""
        return self._get(f"/submission/{submission_id}/status")

    def finalize_submission(self, submission_id: str) -> dict[str, Any]:
        """Finalizes an in-progress submission run to completed status."""
        return self._post(f"/submission/{submission_id}/finalize")

    def submit_batch(self, submission_id: str, answers: list[dict[str, Any]]) -> dict[str, Any]:
        """Submits all epoch answers in a single batch call."""
        return self._post(f"/submission/{submission_id}/submit", {"answers": answers})

    def abort_submission(self, submission_id: str) -> dict[str, Any]:
        """Aborts an active in-progress submission so it is marked interrupted."""
        return self._post(f"/submission/{submission_id}/abort")


class ArenaClient(ToolsClient):
    """Orchestration client for the Agent Arena API.

    Used by main.py to handle:
    - Connection & authentication
    - Task retrieval (get_task)
    - Task submission (submit_task)
    - Submission lifecycle (start_submission, get_submission_status, finalize_submission)
    - Provides scoped ToolsClient for the agent via self.tools.
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = 30.0,
        on_tool_call: Any | None = None,
    ):
        super().__init__(base_url=base_url, token=token, timeout=timeout, on_tool_call=on_tool_call)
        self.tools = ToolsClient(client=self._client, on_tool_call=self._on_tool_call)

    def set_active_task(self, task_id: str) -> None:
        """Sets the active task ID for both the orchestrator and the nested tools client."""
        super().set_active_task(task_id)
        self.tools.set_active_task(task_id)

    def __enter__(self) -> "ArenaClient":
        return self
