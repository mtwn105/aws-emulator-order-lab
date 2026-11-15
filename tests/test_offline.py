"""Business and SDK-contract checks, not evidence of emulator compatibility."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import os
from types import SimpleNamespace

import boto3
from botocore.stub import Stubber

import handler
import local_aws
from workflow import definition


def sdk(service):
    return boto3.client(service, region_name="us-east-1", aws_access_key_id="EMULATOR-test",
                        aws_secret_access_key="test", endpoint_url="https://127.0.0.1:9297")


class OfflineTests(unittest.TestCase):
    def test_emulator_runtime_does_not_read_host_credentials(self):
        local_aws.client.cache_clear()
        with patch.dict(os.environ, {"LAB_FUNCTION_RUNTIME": "1", "LAB_ENDPOINT": "https://127.0.0.1:9397"}), \
                patch.object(local_aws, "sys", SimpleNamespace(implementation=SimpleNamespace(name="rustpython"))), \
                patch.object(local_aws, "connection", side_effect=AssertionError("Host credentials must not be read")), \
                patch.object(local_aws.boto3, "client") as sdk_client:
            local_aws.client("dynamodb")
            sdk_client.assert_called_once_with("dynamodb", endpoint_url="https://127.0.0.1:9397", region_name="us-east-1")
        local_aws.client.cache_clear()

    def test_runtime_flag_cannot_bypass_host_credentials_on_cpython(self):
        local_aws.client.cache_clear()
        with patch.dict(os.environ, {"LAB_FUNCTION_RUNTIME": "1"}), \
                patch.object(local_aws, "sys", SimpleNamespace(implementation=SimpleNamespace(name="cpython"))), \
                patch.object(local_aws, "connection", side_effect=RuntimeError("Credentials required")):
            with self.assertRaisesRegex(RuntimeError, "Credentials required"):
                local_aws.client("dynamodb")
        local_aws.client.cache_clear()

    def test_refuses_cloud_endpoint_before_credentials(self):
        with patch.dict(os.environ, {"LAB_ENDPOINT": "https://s3.amazonaws.com"}):
            with self.assertRaises(ValueError):
                local_aws.connection()

    def test_refuses_non_emulator_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "emulator_credentials").write_text("[default]\naws_access_key_id=AKIA-example\naws_secret_access_key=x\n")
            with patch.dict(os.environ, {"LAB_EMULATOR_DIR": directory, "LAB_ENDPOINT": "https://127.0.0.1:9297"}):
                with self.assertRaises(ValueError):
                    local_aws.connection()

    def test_invalid_quantities_do_not_touch_storage(self):
        ssm = sdk("ssm")
        with Stubber(ssm) as stub:
            for _ in range(5):
                stub.add_response("get_parameter", {"Parameter": {"Value": "5"}}, {"Name": "/orderlab/max-quantity"})
            with patch("handler.client", return_value=ssm) as clients:
                for quantity in (0, -1, 6, True, "2"):
                    response = handler.submit({"body": json.dumps({"orderId": "a", "quantity": quantity})})
                    self.assertEqual(400, response["statusCode"])
                self.assertEqual({call.args[0] for call in clients.call_args_list}, {"ssm"})
            stub.assert_no_pending_responses()

    def test_malformed_json_is_bad_request(self):
        with patch("handler.client") as clients:
            self.assertEqual(400, handler.submit({"body": "{"})["statusCode"])
            clients.assert_not_called()

    def test_first_injected_failure_happens_before_invoice(self):
        db = sdk("dynamodb")
        original = {"status": {"S": "ACCEPTED"}, "failure": {"S": "once"}, "quantity": {"N": "2"}}
        with Stubber(db) as stub:
            stub.add_response("update_item", {"Attributes": {"attempts": {"N": "1"}}}, {
                "TableName": "orderlab-orders", "Key": {"orderId": {"S": "retry"}},
                "UpdateExpression": "ADD attempts :one", "ExpressionAttributeValues": {":one": {"N": "1"}},
                "ReturnValues": "ALL_NEW"})
            with patch("handler.item", return_value=original), patch("handler.log"), patch("handler.client", return_value=db) as clients:
                with self.assertRaises(handler.InjectedFailure):
                    handler.fulfil({"orderId": "retry"})
                self.assertEqual(["dynamodb"], [call.args[0] for call in clients.call_args_list])
            stub.assert_no_pending_responses()

    def test_repeated_fulfil_does_not_rewrite_invoice(self):
        with patch("handler.item", return_value={"status": {"S": "INVOICED"}}), patch("handler.client") as clients:
            self.assertEqual({"orderId": "same"}, handler.fulfil({"orderId": "same"}))
            clients.assert_not_called()

    def test_publish_checks_event_entry_failure(self):
        events = sdk("events")
        with Stubber(events) as stub:
            stub.add_response("put_events", {"FailedEntryCount": 1, "Entries": [{"ErrorCode": "InternalFailure"}]})
            with patch("handler.client", return_value=events), patch("handler.mark") as mark:
                with self.assertRaises(RuntimeError):
                    handler.publish({"orderId": "failed-event"})
                mark.assert_not_called()

    def test_partial_batch_response_preserves_failed_record(self):
        sns = Mock()
        sns.publish.side_effect = [RuntimeError("transient"), {"MessageId": "ok"}]
        records = [{"messageId": str(i), "body": json.dumps({"detail": {"orderId": str(i)}})} for i in (1, 2)]
        with patch("handler.client", return_value=sns), patch("handler.log"):
            self.assertEqual({"batchItemFailures": [{"itemIdentifier": "1"}]}, handler.notify({"Records": records}))
        self.assertEqual(2, sns.publish.call_count)

    def test_workflow_retries_and_failure_context(self):
        workflow = definition("arn:aws:lambda:us-east-1:000000000000:function:orderlab-handler")
        self.assertEqual("CreateInvoice", workflow["StartAt"])
        for name in ("CreateInvoice", "PublishCompletion"):
            state = workflow["States"][name]
            self.assertEqual(2, state["Retry"][0]["MaxAttempts"])
            self.assertEqual("$.error", state["Catch"][0]["ResultPath"])
            self.assertEqual("RecordFailure", state["Catch"][0]["Next"])
        self.assertEqual("Fail", workflow["States"]["Failed"]["Type"])


if __name__ == "__main__":
    unittest.main()
