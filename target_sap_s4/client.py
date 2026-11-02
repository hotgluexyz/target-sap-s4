"""HTTP transport without automatic retries of non-idempotent postings."""

from __future__ import annotations

from functools import cached_property

import requests
from hotglue_singer_sdk.target_sdk.client import HotglueSink


class SapS4RecordSink(HotglueSink):
    """Use a session while keeping SDK bookmarks and error reporting."""

    unified_schema = None

    @property
    def base_url(self):
        """Return the tenant's SAP origin."""
        return self.config["base_url"].rstrip("/")

    @cached_property
    def session(self):
        """Keep SAP credentials confined to this origin; never follow redirects."""
        session = requests.Session()
        session.auth = (self.config["username"], self.config["password"])
        return session

    def send(self, method, path, **kwargs):
        """Make exactly one request; a timeout may mean a posting committed."""
        params = dict(kwargs.pop("params", {}))
        if self.config.get("sap_client"):
            params["sap-client"] = self.config["sap_client"]
        try:
            response = self.session.request(
                method,
                self.base_url + path,
                params=params,
                verify=self.config.get("verify_ssl", True),
                timeout=self.config.get("timeout", 60),
                allow_redirects=False,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                "SAP connection failed; posting outcome may be unknown. Reconcile before retrying."
            ) from exc
        if not 200 <= response.status_code < 300:
            # Do not log response bodies that may contain credentials or customer payloads.
            raise RuntimeError(
                f"SAP returned HTTP {response.status_code}; no automatic retry. "
                "For a posting, reconcile its outcome before retrying."
            )
        return response
