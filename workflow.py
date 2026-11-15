"""Explicit ASL workflow; compile locally, execute with Step Functions."""


def definition(function_arn):
    def task(action, next_state=None, end=False):
        state = {"Type": "Task", "Resource": function_arn,
                 "Parameters": {"action": action, "orderId.$": "$.orderId"},
                 "Retry": [{"ErrorEquals": ["States.TaskFailed"], "IntervalSeconds": 1,
                            "MaxAttempts": 2, "BackoffRate": 2}],
                 "Catch": [{"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "RecordFailure"}]}
        state.update({"End": True} if end else {"Next": next_state})
        return state
    return {"StartAt": "CreateInvoice", "States": {
        "CreateInvoice": task("fulfil", "PublishCompletion"),
        "PublishCompletion": task("publish", end=True),
        "RecordFailure": {"Type": "Task", "Resource": function_arn,
                          "Parameters": {"action": "failed", "orderId.$": "$.orderId"}, "Next": "Failed"},
        "Failed": {"Type": "Fail", "Error": "OrderProcessingFailed"}}}
