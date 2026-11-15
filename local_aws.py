"""Local-only SDK boundary. No default credential chain or cloud fallback."""
import configparser
from functools import lru_cache
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlparse

import boto3
from botocore.config import Config


def endpoint_url():
    endpoint = os.environ.get("LAB_ENDPOINT", "https://127.0.0.1:9297")
    parsed = urlparse(endpoint)
    if (parsed.scheme != "https" or parsed.hostname != "127.0.0.1"
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise ValueError("LAB_ENDPOINT must be an HTTPS endpoint on 127.0.0.1")
    return endpoint


def connection():
    endpoint = endpoint_url()
    parsed = urlparse(endpoint)
    port = parsed.port or 443
    configured = os.environ.get("LAB_EMULATOR_DIR")
    directory = Path(configured) if configured else Path.home() / ".aws/emulator" / str(port)
    parser = configparser.ConfigParser()
    if not parser.read(directory / "emulator_credentials"):
        raise RuntimeError("Start AWS Emulator with authentication enabled first")
    keys = parser["default"]
    access = keys["aws_access_key_id"]
    if not access.startswith("EMULATOR"):
        raise ValueError("Only emulator-issued credentials are accepted")
    ca = directory / "ca.pem"
    if not ca.is_file():
        raise RuntimeError("Missing emulator CA certificate")
    return endpoint, access, keys["aws_secret_access_key"], str(ca)


@lru_cache
def client(service):
    if os.environ.get("LAB_FUNCTION_RUNTIME") == "1" and sys.implementation.name == "rustpython":
        # The emulator supplies its own in-process SDK. It has no host credential files.
        # Keep the endpoint guard; never vendor CPython boto3 over this runtime adapter.
        return boto3.client(service, endpoint_url=endpoint_url(), region_name="us-east-1")
    endpoint, access, secret, ca = connection()
    return boto3.client(
        service, endpoint_url=endpoint, region_name="us-east-1",
        aws_access_key_id=access, aws_secret_access_key=secret, verify=ca,
        config=Config(connect_timeout=3, read_timeout=30, proxies={},
                      retries={"mode": "standard", "total_max_attempts": 2},
                      s3={"addressing_style": "path"}),
    )


def assert_local():
    identity = client("sts").get_caller_identity()
    if identity["Account"] != "000000000000":
        raise RuntimeError("Unexpected account; refusing to continue")


def names():
    prefix = os.environ.get("LAB_PREFIX", "orderlab")
    if not re.fullmatch(r"[a-z][a-z0-9]{2,19}", prefix):
        raise ValueError("LAB_PREFIX must be 3–20 lowercase alphanumeric characters")
    base = f"arn:aws:{{service}}:us-east-1:000000000000:"
    return {
        "prefix": prefix, "table": prefix + "-orders", "bucket": prefix + "-invoices",
        "bus": prefix + "-events", "topic": base.format(service="sns") + prefix + "-completed",
        "function": prefix + "-handler", "machine": base.format(service="states") + "stateMachine:" + prefix,
        "parameter": "/" + prefix + "/max-quantity", "logs": "/" + prefix + "/orders",
        "role": "arn:aws:iam::000000000000:role/" + prefix + "-local",
        "state_role": "arn:aws:iam::000000000000:role/" + prefix + "-workflow",
    }
