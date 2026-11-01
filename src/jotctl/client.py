"""Thin wrapper around the Jotform REST API."""

import json
from collections.abc import Iterator
from enum import StrEnum
from typing import Any

import httpx


class Region(StrEnum):
    """Jotform data regions."""

    US = "us"
    EU = "eu"
    HIPAA = "hipaa"


BASE_URLS = {
    Region.US: "https://api.jotform.com",
    Region.EU: "https://eu-api.jotform.com",
    Region.HIPAA: "https://hipaa-api.jotform.com",
}


class FormStatus(StrEnum):
    """Status of a Jotform form."""

    ENABLED = "ENABLED"
    DISABLED = "DISABLED"
    DELETED = "DELETED"


class FormOrderBy(StrEnum):
    """Fields GET /user/forms can be sorted by (per the API docs)."""

    ID = "id"
    USERNAME = "username"
    TITLE = "title"
    STATUS = "status"
    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"
    NEW = "new"
    COUNT = "count"
    SLUG = "slug"


class JotformError(Exception):
    """Raised when the Jotform API returns an error or can't be reached."""


class JotformClient:
    """
    Client for interacting with the Jotform API.

    Args:
        api_key (str): Jotform API key.
        region (Region, optional): Jotform data region. Defaults to Region.US.
        timeout (float, optional): Request timeout in seconds. Defaults to 30.0.
        transport (httpx.BaseTransport | None, optional): Custom HTTP transport. Defaults to None.
    """

    def __init__(
        self,
        api_key: str,
        region: Region = Region.US,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=BASE_URLS[region],
            headers={"APIKEY": api_key},
            timeout=timeout,
            transport=transport,
        )
        self.limit_left: int | None = None
        """API requests remaining today, from the most recent response's `limit-left`."""

    def __enter__(self) -> "JotformClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> Any:
        try:
            resp = self._http.request(method, path, params=params, data=data, json=json_body)
        except httpx.HTTPError as e:
            raise JotformError(f"Request to Jotform failed: {e}") from e

        try:
            body = resp.json()
        except ValueError:
            raise JotformError(
                f"Unexpected response from Jotform (HTTP {resp.status_code}): {resp.text[:200]}"
            ) from None

        try:
            self.limit_left = int(body["limit-left"])
        except (KeyError, TypeError, ValueError):
            pass

        # Jotform reports errors both via HTTP status and a `responseCode` in the body.
        code = body.get("responseCode", resp.status_code)
        if resp.is_error or code >= 400:
            message = f"Jotform API error ({code}): {body.get('message', 'unknown error')}"
            if code == 401 and method != "GET":
                message += (
                    ". Changing data needs an API key with Full Access; a Read Access key can only read. "
                    "Check the key's permission under Jotform → Settings → API."
                )
            raise JotformError(message)
        return body["content"]

    def list_forms(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        status: FormStatus | None = None,
        order_by: FormOrderBy | None = None,
    ) -> list[dict[str, Any]]:
        """GET /user/forms — forms owned by the API key's account."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status:
            params["filter"] = json.dumps({"status": status.value})
        if order_by:
            params["orderby"] = order_by.value
        return self._get("/user/forms", params)

    def resolve_form_id(self, form: str) -> str:
        """Return the ID of a form given its ID or its title.

        All-digit values are taken as IDs without an API call. Otherwise the account's
        forms are searched for an exact title match, then a case-insensitive one;
        deleted forms are ignored.

        Raises:
            JotformError: If no form, or more than one form, has that title.
        """
        if form.isdigit():
            return form
        forms: list[dict[str, Any]] = []
        page_size = 1000  # the API's maximum
        while True:
            page = self.list_forms(limit=page_size, offset=len(forms))
            forms += page
            if len(page) < page_size:
                break
        forms = [f for f in forms if f.get("status") != FormStatus.DELETED]
        matches = [f for f in forms if f.get("title") == form] or [
            f for f in forms if f.get("title", "").casefold() == form.casefold()
        ]
        if not matches:
            raise JotformError(f'No form titled "{form}" (see `jotctl forms list`)')
        if len(matches) > 1:
            ids = ", ".join(f.get("id", "?") for f in matches)
            raise JotformError(f'{len(matches)} forms are titled "{form}" ({ids}); use the form ID')
        return matches[0]["id"]

    def get_form(self, form_id: str) -> dict[str, Any]:
        """GET /form/{id} — form details (title, status, submission count, ...)."""
        content = self._get(f"/form/{form_id}")
        # The API docs show `content` as a one-element list; accept either shape.
        if isinstance(content, list):
            if not content:
                raise JotformError(f"Form {form_id} not found")
            return content[0]
        return content

    def get_form_properties(self, form_id: str) -> dict[str, Any]:
        """GET /form/{id}/properties — form settings, including its `conditions`."""
        return self._get(f"/form/{form_id}/properties")

    def update_form_properties(self, form_id: str, properties: dict[str, Any]) -> Any:
        """PUT /form/{id}/properties — set form properties; others are left alone.

        Args:
            form_id (str): Form ID.
            properties (dict[str, Any]): Property name → new value. A list-valued property such
                as `conditions` is replaced as a whole.

        Returns:
            Any: The API's `content` (the edited properties).
        """
        return self._request("PUT", f"/form/{form_id}/properties", json_body={"properties": properties})

    def get_form_questions(self, form_id: str) -> dict[str, dict[str, Any]]:
        """GET /form/{id}/questions — question definitions keyed by question ID."""
        return self._get(f"/form/{form_id}/questions")

    def update_question(self, form_id: str, qid: str, properties: dict[str, str]) -> Any:
        """POST /form/{id}/question/{qid} — set properties on one question.

        Args:
            form_id (str): Form ID.
            qid (str): Question ID.
            properties (dict[str, str]): Property name → new value; others are left alone.

        Returns:
            Any: The API's `content` (the updated properties).
        """
        data = {f"question[{prop}]": value for prop, value in properties.items()}
        return self._request("POST", f"/form/{form_id}/question/{qid}", data=data)

    def delete_submission(self, submission_id: str) -> Any:
        """DELETE /submission/{id} — move a submission to the form's trash (status DELETED).

        Deleting a submission that is already in the trash is how Jotform removes it for
        good, though the API docs don't say so; check the result by listing submissions.
        """
        return self._request("DELETE", f"/submission/{submission_id}")

    def iter_submissions(self, form_id: str, *, page_size: int = 1000) -> Iterator[dict[str, Any]]:
        """Yield every submission for a form, paging through GET /form/{id}/submissions.

        Args:
            form_id (str): Jotform form ID.
            page_size (int, optional): Submissions per request (Jotform max is 1000). Defaults to 1000.

        Yields:
            dict[str, Any]: One submission, including its `answers`.
        """
        seen: set[str] = set()
        offset = 0
        while True:
            page = self._get(
                f"/form/{form_id}/submissions",
                {"limit": page_size, "offset": offset, "orderby": "created_at"},
            )
            for submission in page:
                # New submissions arriving mid-pagination shift offsets; skip repeats.
                if submission["id"] not in seen:
                    seen.add(submission["id"])
                    yield submission
            if len(page) < page_size:
                return
            offset += page_size
