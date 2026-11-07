# CloudWatch and Azure Monitor

Both are added in **Connections > Metric sources**, like Prometheus, and you can add several of each, such as one per AWS region or Azure subscription.

## Amazon CloudWatch

### What it reads

| AWS API | Used for |
|---|---|
| `cloudwatch:DescribeAlarms` | Alarms, and which are firing |
| `cloudwatch:ListMetrics`, `cloudwatch:GetMetricData` | The metrics you choose, and the model's metric queries |
| `logs:StartQuery`, `logs:GetQueryResults` | The model's CloudWatch Logs Insights queries |
| `cloudtrail:LookupEvents` | Recent changes, such as deployments and configuration edits |

### Create a read-only identity

Attach the AWS managed policy `CloudWatchReadOnlyAccess`, plus `cloudtrail:LookupEvents` for changes. `CloudWatchReadOnlyAccess` already includes the CloudWatch Logs read actions.

```bash
aws iam create-user --user-name rcc-reader
aws iam attach-user-policy --user-name rcc-reader --policy-arn arn:aws:iam::aws:policy/CloudWatchReadOnlyAccess
aws iam put-user-policy --user-name rcc-reader --policy-name rcc-cloudtrail-read --policy-document \
  '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"cloudtrail:LookupEvents","Resource":"*"}]}'
aws iam create-access-key --user-name rcc-reader
```

When the console runs on EC2 or EKS with an IAM role, leave the keys empty and give the role the same policies. That's safer than keys, which never expire on their own.

### The form

| Field | Example |
|---|---|
| AWS region | `eu-west-1` |
| Access key ID, secret access key | From the commands above, or empty with an IAM role |
| Metrics to track, one per line | `AWS/EC2|CPUUtilization|InstanceId=i-0abc123|Average` |

Each metric line is `namespace|metric|dimension=value|statistic`. Several dimensions are separated with commas: `AWS/ApplicationELB|HTTPCode_Target_5XX_Count|LoadBalancer=app/web/123,TargetGroup=targetgroup/api/456|Sum`.

```bash
# Find metric names and dimensions
aws cloudwatch list-metrics --namespace AWS/EC2 --metric-name CPUUtilization --output table
```

The model writes log queries as `log-group-name|Logs Insights query`, such as `/aws/lambda/checkout|fields @timestamp, @message | filter @message like /ERROR/`.

## Azure Monitor

### What it reads

| Azure API | Used for |
|---|---|
| Alerts Management | Fired alerts |
| Resource Health | Resources that are unavailable or degraded |
| Monitor metrics | The metrics you choose, and the model's metric queries |
| Activity Log | Recent changes, such as deployments and configuration edits |
| Log Analytics query API | The model's KQL queries, if you enter a workspace ID |

### Create a read-only identity

```bash
SUB=$(az account show --query id -o tsv)
az ad sp create-for-rbac --name rcc-azure-reader --role "Monitoring Reader" --scopes /subscriptions/$SUB
```

It prints `appId` (client ID), `password` (client secret), and `tenant` (tenant ID). For log queries, also give it **Log Analytics Reader** on the workspace:

```bash
WS_ID=$(az monitor log-analytics workspace show -g RESOURCE_GROUP -n WORKSPACE --query id -o tsv)
az role assignment create --assignee APP_ID --role "Log Analytics Reader" --scope $WS_ID
az monitor log-analytics workspace show -g RESOURCE_GROUP -n WORKSPACE --query customerId -o tsv   # the workspace ID for the form
```

Scope the role to a resource group instead of the subscription if the console should see less. Client secrets expire (after 1 year by default with this command): note the date, and rotate with `az ad sp credential reset --id APP_ID`.

### The form

| Field | Example |
|---|---|
| Subscription ID | `00000000-0000-0000-0000-000000000000` |
| Log Analytics workspace ID | Optional, the workspace's `customerId` |
| Tenant ID, client ID, client secret | From the commands above |
| Metrics to track, one per line | `/subscriptions/SUB/resourceGroups/RG/providers/Microsoft.Sql/servers/S/databases/D|cpu_percent|Average` |

Each metric line is `resource ID|metric|aggregation`.

```bash
# A resource's ID, and the metrics it offers
az resource show -g RG -n NAME --resource-type Microsoft.Sql/servers/databases --query id -o tsv
az monitor metrics list-definitions --resource RESOURCE_ID --query '[].name.value' -o tsv
```

## Testing status

CloudWatch was tested with the real AWS SDK against a simulator, and Azure Monitor against a fake server that follows Azure's documented API. Try each with a real account on a test subscription first.
