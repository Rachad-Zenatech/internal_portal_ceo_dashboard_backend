# AWS observability

The API writes one JSON object per line to standard output. On ECS or Fargate,
configure the container with the `awslogs` log driver so CloudWatch Logs receives
the stream. Do not add AWS credentials to the application; the ECS task execution
role should have the required log permissions.

Example ECS task-definition fragment:

```json
{
  "logConfiguration": {
    "logDriver": "awslogs",
    "options": {
      "awslogs-group": "/zenatech/production/api",
      "awslogs-region": "ca-central-1",
      "awslogs-stream-prefix": "api"
    }
  }
}
```

Set these application environment variables in the task definition:

- `APP_ENV=production`
- `SERVICE_NAME=zenatech-mcp-server`
- `LOG_LEVEL=INFO`
- `SLOW_REQUEST_MS=2000`

Use `/health/live` for a process/liveness check and `/health/ready` for an ALB
target-group readiness check. Readiness returns HTTP 503 when PostgreSQL is not
available within two seconds.

Every HTTP response includes `X-Request-ID`. Supply that value when reporting a
problem and query it in CloudWatch Logs Insights:

```text
fields @timestamp, level, event, message, path, status_code, duration_ms
| filter request_id = "REQUEST-ID-HERE"
| sort @timestamp asc
```

Find recent backend and browser crashes:

```text
fields @timestamp, service, event, message, request_id, user_id, path
| filter level = "ERROR"
| sort @timestamp desc
| limit 100
```

Deploy `deployment/cloudwatch-observability.yaml` after the log group exists. It
creates an `ErrorCount` metric filter and an alarm. Pass an SNS topic ARN to send
notifications to email, AWS Chatbot/Slack, PagerDuty, or another subscriber.

Example deployment:

```bash
aws cloudformation deploy \
  --template-file deployment/cloudwatch-observability.yaml \
  --stack-name zenatech-observability \
  --parameter-overrides \
    ApplicationLogGroupName=/zenatech/production/api \
    AlarmTopicArn=arn:aws:sns:ca-central-1:123456789012:zenatech-alerts
```

The browser reporter is authenticated, rate-limited in each browser, removes
common token formats, and sends only the pathname and bounded error diagnostics.
It never sends page contents, cookies, request bodies, or query strings.
