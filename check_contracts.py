"""Check SDK operation/argument names offline, including variable client aliases.

This checks top-level names only, not nested values, emulator coverage or service behavior.
"""
import ast
from pathlib import Path
import re
import botocore.session


ALIASES = {"db": "dynamodb", "ssm": "ssm", "sqs": "sqs", "sns": "sns", "states": "stepfunctions",
           "events": "events", "iam": "iam", "lamb": "lambda", "gateway": "apigateway"}


def service_of(node):
    if isinstance(node, ast.Name):
        return ALIASES.get(node.id)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "client"
            and node.args and isinstance(node.args[0], ast.Constant)):
        return node.args[0].value
    return None


def main():
    session = botocore.session.get_session()
    checked = set()
    for filename in ("bootstrap.py", "handler.py", "integration.py", "local_aws.py"):
        tree = ast.parse(Path(filename).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            service = service_of(node.func.value)
            if not service or node.func.attr.startswith("assert_"):
                continue
            method = node.func.attr
            if method == "get_waiter":
                continue
            operation = "".join(part.title() for part in method.split("_"))
            # SDK method names use acronyms; choose by normalized spelling.
            model = session.get_service_model(service)
            matches = [name for name in model.operation_names if re.sub("_", "", name).lower() == operation.lower()]
            if len(matches) != 1:
                raise AssertionError(f"Unknown operation {service}:{method} at {filename}:{node.lineno}")
            operation = matches[0]
            shape = model.operation_model(operation).input_shape
            parameters = {keyword.arg for keyword in node.keywords}
            if None in parameters:
                raise AssertionError("Dynamic keyword arguments need explicit inspection")
            known = set(shape.members) if shape else set()
            required = set(shape.required_members) if shape else set()
            assert parameters <= known, (filename, node.lineno, parameters - known)
            assert required <= parameters, (filename, node.lineno, required - parameters)
            checked.add(service + ":" + operation)
    print(f"Checked {len(checked)} distinct SDK operations: operation names, top-level arguments and required fields")
    Path(".local").mkdir(exist_ok=True)
    Path(".local/operations.json").write_text(__import__("json").dumps(sorted(checked), indent=2) + "\n")


if __name__ == "__main__":
    main()
