"""Render reviewable CloudFormation JSON. Uses only the Python standard library."""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def ref(name):
    return {"Ref": name}


def att(name, attribute):
    return {"Fn::GetAtt": [name, attribute]}


def sub(value):
    return {"Fn::Sub": value}


def resource(kind, **properties):
    return {"Type": "AWS::" + kind, "Properties": properties}


def role(service, statements):
    return resource(
        "IAM::Role",
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}
            ],
        },
        Policies=[
            {
                "PolicyName": "runtime",
                "PolicyDocument": {"Version": "2012-10-17", "Statement": statements},
            }
        ],
    )


def allow(actions, resources):
    return {"Effect": "Allow", "Action": actions, "Resource": resources}


def render():
    bootstrap = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "CTWatch image registry and versioned configuration bucket",
        "Resources": {
            "Repository": resource(
                "ECR::Repository",
                ImageTagMutability="IMMUTABLE",
                ImageScanningConfiguration={"ScanOnPush": True},
                EncryptionConfiguration={"EncryptionType": "AES256"},
            ),
            "ConfigBucket": resource(
                "S3::Bucket",
                VersioningConfiguration={"Status": "Enabled"},
                BucketEncryption={
                    "ServerSideEncryptionConfiguration": [
                        {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                    ]
                },
                PublicAccessBlockConfiguration={
                    "BlockPublicAcls": True,
                    "BlockPublicPolicy": True,
                    "IgnorePublicAcls": True,
                    "RestrictPublicBuckets": True,
                },
            ),
        },
        "Outputs": {
            "RepositoryUri": {"Value": att("Repository", "RepositoryUri")},
            "ConfigBucket": {"Value": ref("ConfigBucket")},
        },
    }
    for entry in bootstrap["Resources"].values():
        entry.update(DeletionPolicy="Retain", UpdateReplacePolicy="Retain")
    r = {}
    r["Vpc"] = resource(
        "EC2::VPC", CidrBlock="10.82.0.0/16", EnableDnsSupport=True, EnableDnsHostnames=True
    )
    r["InternetGateway"] = resource("EC2::InternetGateway")
    r["GatewayAttachment"] = resource(
        "EC2::VPCGatewayAttachment", VpcId=ref("Vpc"), InternetGatewayId=ref("InternetGateway")
    )
    r["RouteTable"] = resource("EC2::RouteTable", VpcId=ref("Vpc"))
    r["DefaultRoute"] = resource(
        "EC2::Route",
        RouteTableId=ref("RouteTable"),
        DestinationCidrBlock="0.0.0.0/0",
        GatewayId=ref("InternetGateway"),
    )
    r["DefaultRoute"]["DependsOn"] = "GatewayAttachment"
    for index in range(2):
        name = f"Subnet{index}"
        r[name] = resource(
            "EC2::Subnet",
            VpcId=ref("Vpc"),
            CidrBlock=f"10.82.{index}.0/24",
            AvailabilityZone={"Fn::Select": [index, {"Fn::GetAZs": ""}]},
        )
        r[name + "Route"] = resource(
            "EC2::SubnetRouteTableAssociation", SubnetId=ref(name), RouteTableId=ref("RouteTable")
        )
    r["SecurityGroup"] = resource(
        "EC2::SecurityGroup",
        VpcId=ref("Vpc"),
        GroupDescription="No inbound access; outbound HTTPS only",
        SecurityGroupEgress=[
            {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "CidrIp": "0.0.0.0/0"}
        ],
    )
    r["State"] = resource(
        "DynamoDB::Table",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "pending", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "PendingIndex",
                "KeySchema": [{"AttributeName": "pending", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        StreamSpecification={"StreamViewType": "NEW_IMAGE"},
        TimeToLiveSpecification={"AttributeName": "expires_at", "Enabled": True},
        SSESpecification={"SSEEnabled": True},
        PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True},
    )
    r["State"].update(DeletionPolicy="Retain", UpdateReplacePolicy="Retain")
    r["Queue"] = resource(
        "SQS::Queue",
        SqsManagedSseEnabled=True,
        MessageRetentionPeriod=1209600,
        VisibilityTimeout=120,
    )
    r["Queue"].update(DeletionPolicy="Retain", UpdateReplacePolicy="Retain")
    r["Logs"] = resource(
        "Logs::LogGroup", LogGroupName=sub("/ctwatch/${AWS::StackName}"), RetentionInDays=14
    )
    r["Cluster"] = resource("ECS::Cluster")
    r["TaskRole"] = role(
        "ecs-tasks.amazonaws.com",
        [
            allow(
                ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:PutItem"], att("State", "Arn")
            ),
            allow(
                ["s3:GetObject"], sub("arn:${AWS::Partition}:s3:::${ConfigBucket}/watchlist.json")
            ),
        ],
    )
    r["ExecutionRole"] = role(
        "ecs-tasks.amazonaws.com",
        [
            allow(["ecr:GetAuthorizationToken"], "*"),
            allow(
                [
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                ],
                sub("arn:${AWS::Partition}:ecr:${AWS::Region}:${AWS::AccountId}:repository/*"),
            ),
            allow(["logs:CreateLogStream", "logs:PutLogEvents"], att("Logs", "Arn")),
        ],
    )
    r["Task"] = resource(
        "ECS::TaskDefinition",
        Cpu=ref("TaskCpu"),
        Memory=ref("TaskMemory"),
        NetworkMode="awsvpc",
        RequiresCompatibilities=["FARGATE"],
        RuntimePlatform={"CpuArchitecture": ref("Architecture"), "OperatingSystemFamily": "LINUX"},
        TaskRoleArn=att("TaskRole", "Arn"),
        ExecutionRoleArn=att("ExecutionRole", "Arn"),
        ContainerDefinitions=[
            {
                "Name": "ctwatch",
                "Image": ref("ImageUri"),
                "Essential": True,
                "ReadonlyRootFilesystem": True,
                "StopTimeout": 120,
                "LinuxParameters": {"InitProcessEnabled": True},
                "MountPoints": [{"SourceVolume": "scratch", "ContainerPath": "/tmp"}],
                "Environment": [
                    {"Name": name, "Value": value}
                    for name, value in {
                        "STATE_TABLE": ref("State"),
                        "AWS_DEFAULT_REGION": ref("AWS::Region"),
                        "WATCHLIST_URI": sub("s3://${ConfigBucket}/watchlist.json"),
                        "WORKERS": ref("Workers"),
                        "POLL_SECONDS": ref("PollSeconds"),
                        "START_MODE": ref("StartMode"),
                    }.items()
                ],
                "LogConfiguration": {
                    "LogDriver": "awslogs",
                    "Options": {
                        "awslogs-group": ref("Logs"),
                        "awslogs-region": ref("AWS::Region"),
                        "awslogs-stream-prefix": "reader",
                        "mode": "non-blocking",
                        "max-buffer-size": "10m",
                    },
                },
                "HealthCheck": {
                    "Command": [
                        "CMD-SHELL",
                        'python -c "import os,time; '
                        "assert time.time()-os.path.getmtime('/tmp/ctwatch-health')<90\"",
                    ],
                    "Interval": 30,
                    "Timeout": 5,
                    "Retries": 3,
                    "StartPeriod": 120,
                },
            }
        ],
        Volumes=[{"Name": "scratch"}],
    )
    r["Service"] = resource(
        "ECS::Service",
        Cluster=ref("Cluster"),
        TaskDefinition=ref("Task"),
        DesiredCount=ref("DesiredCount"),
        LaunchType="FARGATE",
        PlatformVersion="1.4.0",
        EnableECSManagedTags=True,
        DeploymentConfiguration={
            "MinimumHealthyPercent": 0,
            "MaximumPercent": 100,
            "DeploymentCircuitBreaker": {"Enable": True, "Rollback": True},
        },
        NetworkConfiguration={
            "AwsvpcConfiguration": {
                "AssignPublicIp": "ENABLED",
                "Subnets": [ref("Subnet0"), ref("Subnet1")],
                "SecurityGroups": [ref("SecurityGroup")],
            }
        },
    )
    r["Service"]["DependsOn"] = ["DefaultRoute", "Subnet0Route", "Subnet1Route"]
    r["PublisherRole"] = role(
        "lambda.amazonaws.com",
        [
            allow(
                ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator"],
                att("State", "StreamArn"),
            ),
            allow(["dynamodb:ListStreams"], "*"),
            allow(["dynamodb:UpdateItem"], att("State", "Arn")),
            allow(["dynamodb:Query"], sub("${State.Arn}/index/PendingIndex")),
            allow(["sqs:SendMessage"], att("Queue", "Arn")),
            allow(["logs:CreateLogStream", "logs:PutLogEvents"], att("PublisherLogs", "Arn")),
        ],
    )
    r["PublisherLogs"] = resource(
        "Logs::LogGroup",
        LogGroupName=sub("/aws/lambda/${AWS::StackName}-publisher"),
        RetentionInDays=14,
    )
    r["Publisher"] = resource(
        "Lambda::Function",
        FunctionName=sub("${AWS::StackName}-publisher"),
        Runtime="python3.12",
        Handler="index.handler",
        Timeout=60,
        MemorySize=256,
        Role=att("PublisherRole", "Arn"),
        Environment={"Variables": {"STATE_TABLE": ref("State"), "QUEUE_URL": ref("Queue")}},
        Code={"ZipFile": (ROOT / "infra/outbox.py").read_text()},
    )
    r["Publisher"]["DependsOn"] = "PublisherLogs"
    r["StreamMapping"] = resource(
        "Lambda::EventSourceMapping",
        EventSourceArn=att("State", "StreamArn"),
        FunctionName=ref("Publisher"),
        StartingPosition="TRIM_HORIZON",
        BatchSize=100,
        MaximumBatchingWindowInSeconds=1,
        FunctionResponseTypes=["ReportBatchItemFailures"],
        BisectBatchOnFunctionError=True,
        MaximumRetryAttempts=3,
        FilterCriteria={
            "Filters": [
                {
                    "Pattern": json.dumps(
                        {
                            "dynamodb": {
                                "NewImage": {"kind": {"S": ["EVENT"]}, "status": {"S": ["PENDING"]}}
                            }
                        }
                    )
                }
            ]
        },
    )
    r["RecoverySchedule"] = resource(
        "Events::Rule",
        ScheduleExpression="rate(1 minute)",
        State="ENABLED",
        Targets=[{"Id": "publisher", "Arn": att("Publisher", "Arn")}],
    )
    r["RecoveryPermission"] = resource(
        "Lambda::Permission",
        FunctionName=ref("Publisher"),
        Action="lambda:InvokeFunction",
        Principal="events.amazonaws.com",
        SourceArn=att("RecoverySchedule", "Arn"),
    )
    r["ErrorMetric"] = resource(
        "Logs::MetricFilter",
        LogGroupName=ref("Logs"),
        FilterPattern='"ERROR"',
        MetricTransformations=[
            {
                "MetricName": "ReaderErrors",
                "MetricNamespace": "CTWatch",
                "MetricValue": "1",
                "DefaultValue": 0,
                "Dimensions": {"Stack": ref("AWS::StackName")},
            }
        ],
    )
    # Dimension values in metric filters must be JSON selectors, not literal strings.
    r["ErrorMetric"]["Properties"]["MetricTransformations"][0].pop("Dimensions")
    r["ErrorAlarm"] = resource(
        "CloudWatch::Alarm",
        Namespace="CTWatch",
        MetricName="ReaderErrors",
        Statistic="Sum",
        Period=300,
        EvaluationPeriods=1,
        Threshold=1,
        ComparisonOperator="GreaterThanOrEqualToThreshold",
        TreatMissingData="notBreaching",
        AlarmDescription="Inspect CT log errors; cursors stop on failed entries",
    )
    r["PendingAlarm"] = resource(
        "CloudWatch::Alarm",
        Namespace="AWS/SQS",
        MetricName="ApproximateAgeOfOldestMessage",
        Statistic="Maximum",
        Period=300,
        EvaluationPeriods=1,
        Threshold=3600,
        ComparisonOperator="GreaterThanThreshold",
        TreatMissingData="notBreaching",
        Dimensions=[{"Name": "QueueName", "Value": att("Queue", "QueueName")}],
    )
    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "CTWatch continuous Fargate reader, durable cursors and match outbox",
        "Parameters": {
            "ImageUri": {"Type": "String"},
            "ConfigBucket": {"Type": "String"},
            "TaskCpu": {
                "Type": "String",
                "Default": "8192",
                "AllowedValues": ["4096", "8192", "16384"],
            },
            "TaskMemory": {"Type": "String", "Default": "16384"},
            "Architecture": {
                "Type": "String",
                "Default": "ARM64",
                "AllowedValues": ["ARM64", "X86_64"],
            },
            "Workers": {"Type": "String", "Default": "6", "AllowedPattern": "[1-9][0-9]?"},
            "PollSeconds": {"Type": "String", "Default": "15", "AllowedPattern": "[1-9][0-9]*"},
            "DesiredCount": {"Type": "Number", "Default": 0, "AllowedValues": [0, 1]},
            "StartMode": {
                "Type": "String",
                "Default": "head",
                "AllowedValues": ["head", "beginning"],
            },
        },
        "Resources": r,
        "Outputs": {
            "Cluster": {"Value": ref("Cluster")},
            "Service": {"Value": att("Service", "Name")},
            "StateTable": {"Value": ref("State")},
            "QueueUrl": {"Value": ref("Queue")},
            "LogGroup": {"Value": ref("Logs")},
        },
    }
    return {"bootstrap.json": bootstrap, "service.json": template}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for filename, value in render().items():
        path = ROOT / "infra" / filename
        body = json.dumps(value, indent=2) + "\n"
        if args.check:
            if path.read_text() != body:
                raise SystemExit(f"stale template: {filename}; run python infra/render.py")
        else:
            path.write_text(body)
