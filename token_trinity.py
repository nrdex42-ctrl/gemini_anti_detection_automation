"""TokenTrinityManager — dtsg, lsd, jazoest request preparation pipeline.

Every authenticated request to Facebook requires one or more of the three
tokens in the request body or headers. The injection rules differ by request
type:

  - GraphQL mutations: fb_dtsg + lsd + jazoest in form body
  - Form-encoded POST:  fb_dtsg + lsd + jazoest in form body
  - GET requests:       lsd in query params (no jazoest)
  - XHR headers:        lsd in x-fb-lsd header

The jazoest formula (stable since 2019):
    jazoest = (sum of charCodes of all form values, concatenated) % 2199 + 115

See :mod:`jazoest` for the computation.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from .jazoest import compute_jazoest

logger = logging.getLogger(__name__)


class TokenTrinityManager:
    """Prepares requests with the correct dtsg/lsd/jazoest tokens.

    Usage::

        mgr = TokenTrinityManager(dtsg_token="abc", lsd="def")
        body = mgr.prepare_graphql_body(
            doc_id="7711610262198779",
            variables={"input": {...}},
            friendly_name="ComposerStoryCreateMutation",
        )
        # body is ready for urlencode + POST
    """

    def __init__(
        self,
        dtsg_token: str = "",
        lsd: str = "",
        user_id: str = "0",
    ):
        self.dtsg_token = dtsg_token
        self.lsd = lsd
        self.user_id = user_id

    def update_tokens(self, dtsg_token: str = "", lsd: str = "", user_id: str = ""):
        """Refresh tokens from a live page extraction."""
        if dtsg_token:
            self.dtsg_token = dtsg_token
        if lsd:
            self.lsd = lsd
        if user_id:
            self.user_id = user_id

    def prepare_graphql_body(
        self,
        doc_id: str,
        variables: Dict[str, Any],
        friendly_name: str = "ComposerStoryCreateMutation",
    ) -> Dict[str, str]:
        """Build the form-encoded body for a GraphQL mutation.

        Returns a dict ready for ``urllib.parse.urlencode()``.
        The jazoest is computed from ALL values (in insertion order) AFTER
        the body is assembled, matching Facebook's client behavior.
        """
        body = self._base_form()
        body["doc_id"] = doc_id
        body["variables"] = json.dumps(variables)
        body["fb_api_caller_class"] = "RelayModern"
        body["fb_api_req_friendly_name"] = friendly_name
        body["server_timestamps"] = "true"

        # Inject tokens
        body["fb_dtsg"] = self.dtsg_token
        body["lsd"] = self.lsd
        body["__user"] = self.user_id

        # Compute jazoest from all values (jazoest not yet in dict)
        body["jazoest"] = compute_jazoest(body)
        return body

    def prepare_form_body(
        self,
        form_fields: Dict[str, str],
    ) -> Dict[str, str]:
        """Build the form-encoded body for a generic POST.

        Injects fb_dtsg + lsd + __user + jazoest into the form fields.

        The *form_fields* dict should NOT contain fb_dtsg, lsd, __user, or
        jazoest — they are added here.
        """
        body = self._base_form()
        body.update(form_fields)
        body["fb_dtsg"] = self.dtsg_token
        body["lsd"] = self.lsd
        body["__user"] = self.user_id
        body["jazoest"] = compute_jazoest(body)
        return body

    def prepare_get_params(self, extra_params: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """Build query parameters for a GET request.

        Only ``lsd`` is needed for GET (no jazoest, no fb_dtsg).
        """
        params: Dict[str, str] = {}
        if extra_params:
            params.update(extra_params)
        params["lsd"] = self.lsd
        return params

    def get_xhr_headers(self, friendly_name: Optional[str] = None) -> Dict[str, str]:
        """Build XHR headers with lsd for the x-fb-lsd header field."""
        headers: Dict[str, str] = {
            "x-fb-lsd": self.lsd,
        }
        if friendly_name:
            headers["x-fb-friendly-name"] = friendly_name
        return headers

    def _base_form(self) -> Dict[str, str]:
        """Return the standard form fields that appear in every request."""
        return {
            "__a": "1",
            "__req": self._pick_req_id(),
            "__comet_req": "15",
        }

    @staticmethod
    def _pick_req_id() -> str:
        import random
        return random.choice(["2", "3", "4", "5", "6", "7", "8", "9", "a", "b", "c", "d", "e", "f"])

    @property
    def is_ready(self) -> bool:
        """Check if all required tokens are available."""
        return bool(self.dtsg_token) and bool(self.lsd)
