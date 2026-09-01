# AWS EventBridge & EC2 MQTT Architecture Integration Guide

## 1. Executive Summary & Architecture

This document details the architectural integration between the **Event-Driven MQTT Service Availability System** (used by the CEO Dashboard, Administration Portal, and M&A Microservices) and **Amazon EventBridge**.

Connecting MQTT events with EventBridge allows you to:
1. Broadcast service availability (`online`, `offline`, `unknown`) and crash notifications (LWT) to the entire AWS cloud ecosystem.
2. Trigger automated serverless workflows (AWS Lambda, Amazon SNS, SQS, Slack alerts, CloudWatch alarms) when services fail or recover.
3. Forward business domain events (`PURCHASE_APPROVED`, `MA_DEAL_UPDATED`) across decoupled AWS accounts and external SaaS partners.

---

## 2. Architectural Topologies

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ OPTION A: AWS-Native Serverless Pattern (Recommended for Cloud-Native)      │
│                                                                             │
│  [Microservice] ──(MQTT/TLS)──► [AWS IoT Core]                             │
│                                       │                                     │
│                                (IoT Topic Rule)                             │
│                                       ▼                                     │
│                         [Amazon EventBridge Event Bus]                      │
│                                       │                                     │
│            ┌──────────────────────────┼──────────────────────────┐          │
│            ▼                          ▼                          ▼          │
│     [AWS Lambda Alert]         [Amazon SQS Queue]         [Step Functions]  │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│ OPTION B: Hybrid EC2 / Direct Python Bridge Pattern (Fastest Setup)          │
│                                                                             │
│  [Microservices] ──(MQTT:1883)──► [CEO Backend Registry]                   │
│                                             │                               │
│                                      (boto3 put_events)                     │
│                                             ▼                               │
│                               [Amazon EventBridge Event Bus]                │
│                                             │                               │
│                                             ▼                               │
│                                  [Downstream AWS Targets]                   │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Option A: AWS-Native Serverless Integration (AWS IoT Core)

AWS IoT Core acts as a managed, highly available MQTT broker supporting standard MQTT 3.1.1/5.0, QoS 1, Retain, and Last Will and Testament (LWT).

### Step 1: Create an EventBridge Custom Event Bus
1. In the AWS Console, open **Amazon EventBridge** -> **Event Buses**.
2. Click **Create event bus**.
3. Set **Event bus name**: `zenatech-event-bus`.

### Step 2: Configure IoT Core Topic Rule to EventBridge
1. Open **AWS IoT Core** -> **Message routing** -> **Rules**.
2. Click **Create rule**, name: `ForwardMqttToEventBridge`.
3. **SQL Statement**:
   ```sql
   SELECT 
     topic() AS mqtt_topic,
     * 
   FROM 'services/+/+/status'
   ```
4. **Action**: Choose **Amazon EventBridge**.
5. Select Bus: `zenatech-event-bus`.
6. IAM Role: Attach a role granting `events:PutEvents` on `arn:aws:events:*:*:event-bus/zenatech-event-bus`.

---

## 4. Option B: Direct Python Bridge in the CEO Backend

If you are hosting your MQTT broker on an EC2 instance or running the embedded broker, you can forward events directly into EventBridge via the AWS Python SDK (`boto3`).

### 1. EventBridge Publisher Helper
Add this helper module to your backend services:

```python
# services/eventbridge_publisher.py
import json
import logging
import os
import boto3

logger = logging.getLogger(__name__)

# Initialize boto3 EventBridge client
_eventbridge = boto3.client(
    "events",
    region_name=os.getenv("AWS_REGION", "us-west-2")
)

EVENT_BUS_NAME = os.getenv("EVENTBRIDGE_BUS_NAME", "zenatech-event-bus")


def publish_to_eventbridge(source: str, detail_type: str, detail_data: dict) -> bool:
    """
    Publishes an event envelope into the Amazon EventBridge Custom Event Bus.
    """
    if os.getenv("ENABLE_EVENTBRIDGE_FORWARDING", "false").lower() != "true":
        return False

    try:
        response = _eventbridge.put_events(
            Entries=[
                {
                    "EventBusName": EVENT_BUS_NAME,
                    "Source": f"zenatech.{source}",
                    "DetailType": detail_type,
                    "Detail": json.dumps(detail_data),
                }
            ]
        )
        failed_count = response.get("FailedEntryCount", 0)
        if failed_count > 0:
            logger.warning(f"[EventBridge] Failed to publish {failed_count} entries: {response.get('Entries')}")
            return False
        return True
    except Exception as exc:
        logger.warning(f"[EventBridge] Could not publish event: {exc}")
        return False
```

### 2. Connect to Service Status Registry
In `services/service_status_registry.py`, forward state changes:

```python
# When a service status changes:
publish_to_eventbridge(
    source=service,
    detail_type="Service Availability Changed",
    detail_data={
        "service": service,
        "status": status,
        "updatedAt": updated_at,
    }
)

# When a domain business event occurs:
publish_to_eventbridge(
    source=event_data.get("service", "core"),
    detail_type=event_data.get("eventType", "Business Domain Event"),
    detail_data=event_data,
)
```

---

## 5. Event Schema & EventBridge Routing Rules

### 1. Standard EventBridge JSON Envelope
Every event delivered to EventBridge follows this structured format:

```json
{
  "version": "0",
  "id": "c1f72005-4c07-b248-d309-847e06821213",
  "detail-type": "Service Availability Changed",
  "source": "zenatech.admin",
  "account": "123456789012",
  "time": "2026-09-01T16:30:00Z",
  "region": "us-west-2",
  "resources": [],
  "detail": {
    "service": "admin",
    "instanceId": "admin-01",
    "status": "offline",
    "reason": "connection-lost",
    "occurredAt": "2026-09-01T16:30:00Z"
  }
}
```

### 2. Example EventBridge Filter Rules

#### Rule 1: Alert on Service Outage (Trigger Lambda / SNS / PagerDuty)
* **Pattern**:
  ```json
  {
    "source": [{"prefix": "zenatech."}],
    "detail-type": ["Service Availability Changed"],
    "detail": {
      "status": ["offline"]
    }
  }
  ```

#### Rule 2: Purchase Request Approved (Trigger SQS for ERP Sync)
* **Pattern**:
  ```json
  {
    "source": ["zenatech.admin"],
    "detail-type": ["PURCHASE_APPROVED"]
  }
  ```

---

## 6. AWS Security & IAM Policies

### EC2 IAM Instance Role Policy
Attach this policy to the IAM Role assigned to your EC2 instance:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowEventBridgePutEvents",
      "Effect": "Allow",
      "Action": [
        "events:PutEvents"
      ],
      "Resource": "arn:aws:events:us-west-2:*:event-bus/zenatech-event-bus"
    }
  ]
}
```

---

## 7. Environment Configuration Reference

Add these variables to your `.env` file when enabling EventBridge:

```env
# ==============================================================================
# AWS EventBridge Integration Settings
# ==============================================================================
ENABLE_EVENTBRIDGE_FORWARDING=true
EVENTBRIDGE_BUS_NAME=zenatech-event-bus
AWS_REGION=us-west-2
```
