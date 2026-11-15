"""Handlers shared by API Gateway, Step Functions and SQS."""
import json
import time
import re

from local_aws import client, names


class InjectedFailure(Exception):
    pass


def item(order_id):
    response = client("dynamodb").get_item(
        TableName=names()["table"], Key={"orderId": {"S": order_id}}, ConsistentRead=True)
    return response.get("Item")


def mark(order_id, status):
    client("dynamodb").update_item(
        TableName=names()["table"], Key={"orderId": {"S": order_id}},
        UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": {"S": status}})


def log(order_id, stage):
    # Explicit log writes keep application evidence distinct from automatic runtime logs.
    client("logs").put_log_events(
        logGroupName=names()["logs"], logStreamName="orders",
        logEvents=[{"timestamp": int(time.time() * 1000),
                    "message": json.dumps({"orderId": order_id, "stage": stage})}])


def reply(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body)}


def submit(event):
    try:
        order = json.loads(event.get("body") or "{}")
        if not isinstance(order, dict):
            raise ValueError("Expected an object")
        order_id = order.get("orderId", "")
        quantity = order.get("quantity")
        mode = order.get("failure", "none")
        maximum = int(client("ssm").get_parameter(Name=names()["parameter"])["Parameter"]["Value"])
        if (not isinstance(order_id, str) or not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", order_id)
                or type(quantity) is not int or not 1 <= quantity <= maximum
                or mode not in ("none", "once", "always")):
            raise ValueError("Invalid orderId, quantity or failure mode")
    except (ValueError, TypeError):
        return reply(400, {"error": "Use orderId (1–64 letters/digits/hyphens), quantity within the configured limit, and failure none/once/always"})

    db = client("dynamodb")
    try:
        db.put_item(TableName=names()["table"], Item={
            "orderId": {"S": order_id}, "quantity": {"N": str(quantity)},
            "failure": {"S": mode}, "status": {"S": "ACCEPTED"}, "attempts": {"N": "0"}},
            ConditionExpression="attribute_not_exists(orderId)")
    except db.exceptions.ConditionalCheckFailedException:
        existing = item(order_id)
        if existing["quantity"]["N"] != str(quantity) or existing["failure"]["S"] != mode:
            return reply(409, {"error": "orderId already used with different input"})

    # Retrying submission repairs the record-created/execution-not-started gap.
    states = client("stepfunctions")
    try:
        states.start_execution(stateMachineArn=names()["machine"], name=order_id,
                               input=json.dumps({"orderId": order_id}, sort_keys=True))
    except states.exceptions.ExecutionAlreadyExists:
        pass  # Same logical order, not a request to restart a failed execution.
    return reply(202, {"orderId": order_id})


def fulfil(event):
    order_id = event["orderId"]
    existing = item(order_id)
    if existing is None:
        raise ValueError("Unknown order")
    if existing["status"]["S"] in ("INVOICED", "EVENT_ACCEPTED"):
        return {"orderId": order_id}
    response = client("dynamodb").update_item(
        TableName=names()["table"], Key={"orderId": {"S": order_id}},
        UpdateExpression="ADD attempts :one", ExpressionAttributeValues={":one": {"N": "1"}},
        ReturnValues="ALL_NEW")
    attempt = int(response["Attributes"]["attempts"]["N"])
    log(order_id, f"fulfil-attempt-{attempt}")
    if existing["failure"]["S"] == "always" or (existing["failure"]["S"] == "once" and attempt == 1):
        raise InjectedFailure("Deliberate failure before invoice creation")
    invoice = {"orderId": order_id, "quantity": int(existing["quantity"]["N"]),
               "totalCents": int(existing["quantity"]["N"]) * 2500, "currency": "USD"}
    client("s3").put_object(Bucket=names()["bucket"], Key=order_id + ".json",
                            Body=json.dumps(invoice).encode(), ContentType="application/json")
    mark(order_id, "INVOICED")
    return {"orderId": order_id}


def publish(event):
    order_id = event["orderId"]
    response = client("events").put_events(Entries=[{
        "EventBusName": names()["bus"], "Source": "orderlab.orders", "DetailType": "OrderCompleted",
        "Detail": json.dumps({"orderId": order_id})}])
    if response.get("FailedEntryCount", 0):
        raise RuntimeError("EventBridge rejected an entry")
    mark(order_id, "EVENT_ACCEPTED")
    log(order_id, "completion-event-accepted")
    return {"orderId": order_id}


def notify(event):
    failed = []
    for record in event["Records"]:
        try:
            detail = json.loads(record["body"])["detail"]
            client("sns").publish(TopicArn=names()["topic"], Message=json.dumps(detail))
            log(detail["orderId"], "notification-published")
        except Exception:
            # Per-record failure must stay visible to the SQS/Lambda integration.
            failed.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failed}


def handler(event, context):
    if "Records" in event:
        return notify(event)
    action = event.get("action")
    if action == "fulfil":
        return fulfil(event)
    if action == "publish":
        return publish(event)
    if action == "failed":
        mark(event["orderId"], "FAILED")
        log(event["orderId"], "workflow-failed")
        return {"orderId": event["orderId"]}
    return submit(event)
