"""Submit one order through the emulated API Gateway and show the result."""
import argparse
import json
from pathlib import Path
import time
import uuid

from local_aws import assert_local, client, names


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quantity", type=int, default=2, help="units to invoice (default: 2)")
    parser.add_argument("--failure", choices=("none", "once", "always"), default="none",
                        help="inject a failure before invoice creation (default: none)")
    parser.add_argument("--order-id", help="reuse an order ID instead of generating one")
    args = parser.parse_args()

    if Path(".local/resources.json").is_file():
        resources = json.loads(Path(".local/resources.json").read_text())
        if resources["names"] != names():
            raise SystemExit("LAB_PREFIX does not match bootstrap output; rerun bootstrap.py")
    else:
        raise SystemExit("No resources found; run bootstrap.py first")

    assert_local()
    order_id = args.order_id or "demo-" + uuid.uuid4().hex[:8]
    order = {"orderId": order_id, "quantity": args.quantity, "failure": args.failure}
    reply = client("apigateway").test_invoke_method(
        restApiId=resources["api"], resourceId=resources["resource"],
        httpMethod="POST", body=json.dumps(order))
    print(f"HTTP {reply['status']} {reply['body']}")
    if reply["status"] != 202:
        return 1

    arn = resources["machine"].replace(":stateMachine:", ":execution:") + ":" + order_id
    deadline = time.monotonic() + 60
    status = "RUNNING"
    while time.monotonic() < deadline:
        status = client("stepfunctions").describe_execution(executionArn=arn)["status"]
        if status != "RUNNING":
            break
        time.sleep(0.5)
    print(f"workflow: {status}")
    print(f"Inspect it with: python inspect_order.py {order_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
