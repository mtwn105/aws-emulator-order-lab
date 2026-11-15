"""Local workbench for the emulated order lab: JSON API plus a static dashboard.

Runs against the same emulator, credentials and resources as bootstrap.py and
integration.py. It never calls AWS outside the configured loopback endpoint.
"""
import argparse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlparse
import uuid

from local_aws import assert_local, client, names

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
RESOURCES = ROOT / ".local" / "resources.json"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
INBOX_NAMES = ("customer", "audit")


class LabError(Exception):
    """An error the dashboard can explain to the person using it."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def invoice_cents(quantity):
    """Invoice total for one order, in US cents."""
    return int(quantity) * 2500


def validate_order(payload, limit):
    """Return an error message, or None when the payload is acceptable."""
    order_id = payload.get("orderId", "")
    quantity = payload.get("quantity")
    mode = payload.get("failure", "none")
    if not isinstance(order_id, str) or not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", order_id):
        return "Use 1–64 letters, digits or hyphens for the order ID."
    if type(quantity) is not int or not 1 <= int(quantity) <= int(limit):
        return f"Quantity must be a whole number from 1 to {limit}."
    if mode not in ("none", "once", "always"):
        return "Choose failure none, once or always."
    return None


def order_row(item, execution, invoice_key):
    """Shape one order for the ledger table."""
    quantity = int(item["quantity"]["N"]) if item else None
    return {
        "id": item["orderId"]["S"] if item else (execution or {}).get("name"),
        "quantity": quantity,
        "failure": item["failure"]["S"] if item else None,
        "status": item["status"]["S"] if item else None,
        "attempts": int(item["attempts"]["N"]) if item else None,
        "executionStatus": (execution or {}).get("status"),
        "startedAt": iso((execution or {}).get("startDate")),
        "stoppedAt": iso((execution or {}).get("stopDate")),
        "invoiceCents": invoice_cents(quantity) if invoice_key else None,
    }


def iso(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return None


def resources():
    if not RESOURCES.is_file():
        raise LabError(HTTPStatus.SERVICE_UNAVAILABLE,
                       "No lab resources found. Run python bootstrap.py with the same LAB_* settings.")
    data = json.loads(RESOURCES.read_text())
    if data["names"] != names():
        raise LabError(HTTPStatus.SERVICE_UNAVAILABLE,
                       "LAB_PREFIX does not match .local/resources.json. Rerun bootstrap.py or fix LAB_PREFIX.")
    return data


def guard(handler):
    """Turn emulator and configuration failures into readable JSON errors."""
    try:
        return handler()
    except LabError as error:
        return send_json(error.status, {"error": error.message})
    except Exception as error:  # noqa: BLE001 - surfaced to the local operator
        name = type(error).__name__
        return send_json(HTTPStatus.BAD_GATEWAY,
                         {"error": f"{name}: {error}", "hint": "Is the emulator running on LAB_ENDPOINT?"})


def send_json(status, payload):
    body = json.dumps(payload, default=str).encode()
    return status, {"Content-Type": "application/json"}, body


def executions(limit=50):
    found = client("stepfunctions").list_executions(
        stateMachineArn=names()["machine"], maxResults=limit).get("executions", [])
    return {entry["name"]: entry for entry in found}


def stored_orders(order_ids):
    if not order_ids:
        return {}
    table = names()["table"]
    db = client("dynamodb")
    found = {}
    for start in range(0, len(order_ids), 100):
        chunk = order_ids[start:start + 100]
        response = db.batch_get_item(RequestItems={table: {"Keys": [
            {"orderId": {"S": order_id}} for order_id in chunk]}})
        for item in response.get("Responses", {}).get(table, []):
            found[item["orderId"]["S"]] = item
    return found


def invoice_keys():
    bucket = names()["bucket"]
    keys = []
    token = None
    while True:
        kwargs = {"Bucket": bucket, "MaxKeys": 1000}
        if token:
            kwargs["ContinuationToken"] = token
        page = client("s3").list_objects_v2(**kwargs)
        keys.extend(obj["Key"] for obj in page.get("Contents", []))
        if not page.get("IsTruncated"):
            return keys
        token = page.get("NextContinuationToken")


def queue_snapshot(url):
    attrs = client("sqs").get_queue_attributes(
        QueueUrl=url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
    return {"depth": int(attrs.get("ApproximateNumberOfMessages", 0)),
            "inFlight": int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0))}


def api_lab():
    lab = resources()
    runs = executions(limit=200)
    keys = invoice_keys()
    finished = [r for r in runs.values() if r["status"] != "RUNNING"]
    return send_json(HTTPStatus.OK, {
        "prefix": lab["names"]["prefix"],
        "endpoint": client("sts").meta.endpoint_url,
        "limit": int(client("ssm").get_parameter(Name=lab["names"]["parameter"])["Parameter"]["Value"]),
        "counts": {
            "total": len(runs),
            "running": sum(1 for r in runs.values() if r["status"] == "RUNNING"),
            "failed": sum(1 for r in runs.values() if r["status"] == "FAILED"),
            "succeeded": sum(1 for r in finished if r["status"] == "SUCCEEDED"),
            "invoices": len(keys),
        },
    })


def api_orders():
    lab = resources()
    runs = executions()
    items = stored_orders(list(runs))
    keys = set(invoice_keys())
    rows = [order_row(items.get(name), run, name + ".json" in keys)
            for name, run in runs.items()]
    rows.sort(key=lambda row: row["startedAt"] or "", reverse=True)
    return send_json(HTTPStatus.OK, {"orders": rows, "prefix": lab["names"]["prefix"]})


def api_order(order_id):
    lab = resources()
    if not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", order_id):
        raise LabError(HTTPStatus.BAD_REQUEST, "Invalid order ID.")
    db = client("dynamodb")
    item = db.get_item(TableName=names()["table"], Key={"orderId": {"S": order_id}},
                       ConsistentRead=True).get("Item")
    states = client("stepfunctions")
    arn = names()["machine"].replace(":stateMachine:", ":execution:") + ":" + order_id
    try:
        execution = states.describe_execution(executionArn=arn)
    except states.exceptions.ExecutionDoesNotExist:
        execution = None
    if item is None and execution is None:
        raise LabError(HTTPStatus.NOT_FOUND, "No order or execution with that ID.")

    invoice = None
    try:
        body = client("s3").get_object(Bucket=names()["bucket"], Key=order_id + ".json")["Body"]
        invoice = json.loads(body.read())
    except client("s3").exceptions.NoSuchKey:
        pass

    inboxes = {}
    sqs = client("sqs")
    for name, inbox in zip(INBOX_NAMES, lab["inboxes"]):
        seen = False
        response = sqs.receive_message(QueueUrl=inbox["url"], MaxNumberOfMessages=10,
                                       WaitTimeSeconds=0, VisibilityTimeout=0)
        for message in response.get("Messages", []):
            try:
                seen = seen or json.loads(message["Body"]).get("orderId") == order_id
            except json.JSONDecodeError:
                continue
        inboxes[name] = seen

    events = client("logs").filter_log_events(
        logGroupName=names()["logs"], filterPattern=f'"{order_id}"', limit=20).get("events", [])
    return send_json(HTTPStatus.OK, {
        "order": item and {
            "id": item["orderId"]["S"], "quantity": int(item["quantity"]["N"]),
            "failure": item["failure"]["S"], "status": item["status"]["S"],
            "attempts": int(item["attempts"]["N"])},
        "execution": execution and {"status": execution["status"],
                                    "startedAt": iso(execution.get("startDate")),
                                    "stoppedAt": iso(execution.get("stopDate"))},
        "invoice": invoice,
        "inboxes": inboxes,
        "logs": [{"time": iso(datetime.fromtimestamp(event["timestamp"] / 1000, timezone.utc)),
                  "message": event["message"]} for event in events],
    })


def api_submit(payload):
    lab = resources()
    limit = int(client("ssm").get_parameter(Name=lab["names"]["parameter"])["Parameter"]["Value"])
    order_id = payload.get("orderId") or "ui-" + uuid.uuid4().hex[:8]
    body = {"orderId": order_id, "quantity": payload.get("quantity"),
            "failure": payload.get("failure", "none")}
    problem = validate_order(body, limit)
    if problem:
        raise LabError(HTTPStatus.BAD_REQUEST, problem)
    reply = client("apigateway").test_invoke_method(
        restApiId=lab["api"], resourceId=lab["resource"], httpMethod="POST",
        body=json.dumps(body))
    parsed = json.loads(reply.get("body") or "{}")
    if reply["status"] not in (200, 202):
        raise LabError(reply["status"], parsed.get("error", "The submission was rejected."))
    return send_json(HTTPStatus.ACCEPTED, {"orderId": body["orderId"], "status": reply["status"]})


def api_system():
    lab = resources()
    queues = [{"name": "completion bus", **queue_snapshot(lab["queue"]["url"])}]
    for name, inbox in zip(INBOX_NAMES, lab["inboxes"]):
        queues.append({"name": f"{name} inbox", **queue_snapshot(inbox["url"])})
    if lab.get("dlq"):
        queues.append({"name": "dead letters", **queue_snapshot(lab["dlq"]["url"])})
    events = client("logs").filter_log_events(logGroupName=names()["logs"], limit=25).get("events", [])
    events.reverse()
    return send_json(HTTPStatus.OK, {
        "queues": queues,
        "logs": [{"time": iso(datetime.fromtimestamp(event["timestamp"] / 1000, timezone.utc)),
                  "message": event["message"]} for event in events],
        "parameter": {"name": names()["parameter"],
                      "value": client("ssm").get_parameter(Name=names()["parameter"])["Parameter"]["Value"]},
    })


class Handler(BaseHTTPRequestHandler):
    server_version = "OrderLab/0.2"

    def log_message(self, fmt, *args):  # keep the terminal readable
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def send(self, status, headers, body):
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - stdlib hook
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            status, headers, body = guard(lambda: self.api_get(path))
            return self.send(status, headers, body)
        entry = STATIC.get(path)
        if entry is None:
            return self.send(*send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"}))
        filename, content_type = entry
        data = (WEB / filename).read_bytes()
        self.send(HTTPStatus.OK, {"Content-Type": content_type}, data)

    def do_POST(self):  # noqa: N802 - stdlib hook
        path = urlparse(self.path).path
        if path != "/api/orders":
            return self.send(*send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"}))
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            return self.send(*send_json(HTTPStatus.BAD_REQUEST, {"error": "Expected a JSON object."}))
        status, headers, body = guard(lambda: api_submit(payload))
        self.send(status, headers, body)

    def api_get(self, path):
        if path == "/api/lab":
            return api_lab()
        if path == "/api/orders":
            return api_orders()
        if path == "/api/system":
            return api_system()
        match = re.fullmatch(r"/api/orders/([^/]+)", path)
        if match:
            return api_order(unquote(match.group(1)))
        return send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9410)
    args = parser.parse_args()
    try:
        resources()
        assert_local()
        print(f"Lab ready: prefix {names()['prefix']} at {client('sts').meta.endpoint_url}")
    except Exception as error:  # noqa: BLE001 - the page shows what to fix
        print(f"Warning: lab not ready ({error}). The dashboard will show the error until it is.")
    print(f"Dashboard: http://{args.host}:{args.port}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
