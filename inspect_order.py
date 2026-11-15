"""Inspect an order without an AWS Console or a browser frontend."""
import argparse
import json

from handler import item
from local_aws import assert_local, client, names


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("order_id")
    args = parser.parse_args()
    assert_local()
    order = item(args.order_id)
    if order is None:
        raise SystemExit("Order not found")
    output = {"order": order}
    if order["status"]["S"] in ("INVOICED", "EVENT_ACCEPTED"):
        with client("s3").get_object(Bucket=names()["bucket"], Key=args.order_id + ".json")["Body"] as body:
            output["invoice"] = json.loads(body.read())
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
