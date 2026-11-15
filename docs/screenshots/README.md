# Screenshots

Actual, unedited captures of the running local order lab and StackPort on 2026-11-15 UTC.

App: port 9410. StackPort 0.4.11: port 8080. Both inspected the existing emulator at https://127.0.0.1:9401 with prefix order385. The README quick start uses default port 9297 and prefix orderlab.

Capture timestamps, dimensions, hashes and limitations are in [manifest.json](manifest.json). StackPort is an independent project; see its [AWS Emulator guide](https://stackport.cloud/docs/guides/aws-emulator/).

## Retry-once order ui-bcd16a55; system trace expanded

![Retry-once order ui-bcd16a55; system trace expanded](app-retry-success.png)

## Permanent-failure order ui-4670ce2b

![Permanent-failure order ui-4670ce2b](app-permanent-failure.png)

## 390-pixel viewport; ui-bcd16a55 selected

![390-pixel viewport; ui-bcd16a55 selected](app-mobile.png)

## Service dashboard

![Service dashboard](stackport-dashboard.png)

## Order item browser

![Order item browser](stackport-dynamodb-items.png)

## Four lab queues

![Four lab queues](stackport-sqs-queues.png)

## order385-customer selected; Poll for messages

![order385-customer selected; Poll for messages](stackport-sqs-messages.png)

## order385-invoices/ui-bcd16a55.json metadata; Raw tab

![order385-invoices/ui-bcd16a55.json metadata; Raw tab](stackport-s3-invoice.png)

## State machine order385 selected; execution list

![State machine order385 selected; execution list](stackport-workflow-executions.png)

## Filter pattern "ui-bcd16a55"; four matching entries

![Filter pattern "ui-bcd16a55"; four matching entries](stackport-order-logs.png)
