"""Strict integration assertions. Never substitute a direct call for a missing trigger."""
import json
from pathlib import Path
import time
import uuid
from datetime import datetime, timezone

from handler import item
from local_aws import assert_local, client, names


def wait_for(check, description, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.5)
    raise AssertionError("Timed out: " + description)


def submit(resources, order):
    return client("apigateway").test_invoke_method(
        restApiId=resources["api"], resourceId=resources["resource"], httpMethod="POST", body=json.dumps(order))


def execution_status(resources, order_id):
    arn = resources["machine"].replace(":stateMachine:", ":execution:") + ":" + order_id
    result = client("stepfunctions").describe_execution(executionArn=arn)
    return result["status"] if result["status"] != "RUNNING" else None


def observe_notification(inbox, order_id):
    sqs = client("sqs")
    response = sqs.receive_message(QueueUrl=inbox["url"], MaxNumberOfMessages=10, WaitTimeSeconds=1)
    found = False
    for message in response.get("Messages", []):
        if json.loads(message["Body"]).get("orderId") == order_id:
            found = True
        sqs.delete_message(QueueUrl=inbox["url"], ReceiptHandle=message["ReceiptHandle"])
    return found


def run():
    assert_local()
    resources = json.loads(Path(".local/resources.json").read_text())
    if resources["names"] != names():
        raise ValueError("LAB_PREFIX does not match bootstrap output")
    run_id = uuid.uuid4().hex[:10]
    evidence = {"runId": run_id, "checks": [], "orders": [],
                "startedAt": datetime.now(timezone.utc).isoformat(),
                "apiMode": "API Gateway TestInvokeMethod (not a deployed HTTP URL)"}
    def passed(name):
        evidence["checks"].append(name)
        print("PASS", name)

    invalid = run_id + "-invalid"
    assert submit(resources, {"orderId": invalid, "quantity": 0})["status"] == 400
    assert item(invalid) is None
    passed("invalid quantity rejected without storing an order")

    for mode, expected_attempts in (("none", 1), ("once", 2), ("always", 3)):
        order_id = run_id + "-" + mode
        order = {"orderId": order_id, "quantity": 2, "failure": mode}
        assert submit(resources, order)["status"] == 202
        status = wait_for(lambda: execution_status(resources, order_id), "workflow " + order_id)
        stored = item(order_id)
        assert int(stored["attempts"]["N"]) == expected_attempts, stored
        observation = {"orderId": order_id, "mode": mode, "executionStatus": status,
                       "attempts": int(stored["attempts"]["N"]), "orderStatus": stored["status"]["S"],
                       "invoice": None, "notificationInboxes": []}
        evidence["orders"].append(observation)
        if mode == "always":
            assert status == "FAILED" and stored["status"]["S"] == "FAILED"
            try:
                client("s3").get_object(Bucket=names()["bucket"], Key=order_id + ".json")
            except client("s3").exceptions.NoSuchKey:
                pass
            else:
                raise AssertionError("Permanent failure produced an invoice")
            passed("permanent failure exhausted three attempts and created no invoice")
            continue
        assert status == "SUCCEEDED" and stored["status"]["S"] == "EVENT_ACCEPTED"
        with client("s3").get_object(Bucket=names()["bucket"], Key=order_id + ".json")["Body"] as body:
            invoice = json.loads(body.read())
        assert invoice == {"orderId": order_id, "quantity": 2, "totalCents": 5000, "currency": "USD"}
        observation["invoice"] = invoice
        passed(mode + ": workflow, attempt count, stored state and invoice verified")
        for index, inbox in enumerate(resources["inboxes"]):
            wait_for(lambda: observe_notification(inbox, order_id), "EventBridge → SQS → Lambda → SNS → inbox")
            observation["notificationInboxes"].append(index + 1)
            passed(mode + f": notification observed in inbox {index + 1}")
        assert submit(resources, order)["status"] == 202
        assert int(item(order_id)["attempts"]["N"]) == expected_attempts
        assert submit(resources, {**order, "quantity": 3})["status"] == 409
        passed(mode + ": repeated submission preserved attempts; conflicting input rejected")

    client("ssm").put_parameter(Name=names()["parameter"], Type="String", Value="1", Overwrite=True)
    try:
        assert submit(resources, {"orderId": run_id + "-limit", "quantity": 2})["status"] == 400
    finally:
        client("ssm").put_parameter(Name=names()["parameter"], Type="String", Value="5", Overwrite=True)
    passed("configuration change affected validation")
    def logs_arrived():
        response = client("logs").filter_log_events(logGroupName=names()["logs"], filterPattern='"' + run_id + '-once"')
        return any("notification-published" in entry["message"] for entry in response.get("events", []))
    wait_for(logs_arrived, "correlated CloudWatch application log")
    passed("correlated application log found")
    evidence["finishedAt"] = datetime.now(timezone.utc).isoformat()
    return evidence


def main():
    target = Path(".local/integration-results.json")
    target.parent.mkdir(exist_ok=True)
    try:
        evidence = run()
    except Exception as error:
        target.write_text(json.dumps({"status": "FAILED", "errorType": type(error).__name__}, indent=2) + "\n")
        raise
    target.write_text(json.dumps({"status": "PASSED", **evidence}, indent=2) + "\n")


if __name__ == "__main__":
    main()
