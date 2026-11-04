"""Singer target for the standard SAP S/4HANA journal-entry API."""

from __future__ import annotations

from urllib.parse import urlsplit

from hotglue_singer_sdk.target_sdk.target import TargetHotglue

from target_sap_s4.sinks import JournalEntriesSink


class TargetSapS4(TargetHotglue):
    """Create journal entries; never update existing financial documents."""

    name = "target-sap-s4"
    SINK_TYPES = [JournalEntriesSink]
    MAX_PARALLELISM = 1
    config_jsonschema = {
        "type": "object",
        "required": ["base_url", "username", "password"],
        "properties": {
            "base_url": {"type": "string", "description": "SAP HTTPS origin, including port"},
            "username": {"type": "string", "minLength": 1},
            "password": {"type": "string", "minLength": 1, "secret": True},
            "sap_client": {"type": "string", "pattern": "^[0-9]{3}$"},
            "verify_ssl": {"type": "boolean", "default": True},
            "test_data_indicator": {"type": "boolean", "default": True},
            "timeout": {"type": "integer", "minimum": 1, "default": 60},
        },
    }

    def __init__(self, *args, **kwargs):
        """Reject credential-bearing URLs and path/query ambiguity before HTTP."""
        super().__init__(*args, **kwargs)
        url = urlsplit(self.config["base_url"])
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.path not in ("", "/")
            or url.query
            or url.fragment
        ):
            raise ValueError("base_url must be an HTTPS origin without credentials, path or query")

    def get_sink_class(self, stream_name):
        """Fail on unsupported streams instead of silently dropping recoveries."""
        if stream_name != JournalEntriesSink.name:
            raise ValueError(f"Unsupported SAP stream: {stream_name}")
        return JournalEntriesSink


if __name__ == "__main__":
    TargetSapS4.cli()
