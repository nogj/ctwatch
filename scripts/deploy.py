"""Explicit AWS lifecycle: bootstrap, upload, build, plan, apply, status. Never auto-deploy."""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from ctwatch.config import DEFAULT_POLL_SECONDS, positive_int

ROOT = Path(__file__).resolve().parents[1]


def outputs(cf, stack):
    result = cf.describe_stacks(StackName=stack)["Stacks"][0]
    return {item["OutputKey"]: item["OutputValue"] for item in result.get("Outputs", [])}


def exists(cf, stack):
    try:
        cf.describe_stacks(StackName=stack)
        return True
    except ClientError as error:
        if "does not exist" not in str(error):
            raise
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    parser.add_argument("--region", default="eu-west-1")
    parser.add_argument("--name", default="ctwatch")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("bootstrap", help="Create ECR + configuration bucket (billable resources)")
    upload = commands.add_parser("upload-watchlist")
    upload.add_argument("file", type=Path)
    build = commands.add_parser("build", help="Build and push an immutable image to ECR")
    build.add_argument("--architecture", choices=["ARM64", "X86_64"], default="ARM64")
    plan = commands.add_parser("plan", help="Create, inspect and save a CloudFormation change set")
    plan.add_argument("--image", required=True)
    plan.add_argument("--desired-count", type=int, choices=[0, 1], default=0)
    plan.add_argument("--cpu", type=int, choices=[4096, 8192, 16384], default=8192)
    plan.add_argument("--memory", type=int, default=16384)
    plan.add_argument("--workers", type=int, default=6)
    plan.add_argument("--poll-seconds", type=positive_int, default=DEFAULT_POLL_SECONDS)
    plan.add_argument("--architecture", choices=["ARM64", "X86_64"], default="ARM64")
    apply = commands.add_parser("apply", help="Execute a previously reviewed change set")
    apply.add_argument("--change-set", required=True)
    commands.add_parser("status")
    args = parser.parse_args()
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    cf = session.client("cloudformation")
    bootstrap_stack = args.name + "-bootstrap"
    print(f"Account={account} region={args.region} stack={args.name}", flush=True)
    if args.command == "bootstrap":
        if not exists(cf, bootstrap_stack):
            cf.create_stack(
                StackName=bootstrap_stack, TemplateBody=(ROOT / "infra/bootstrap.json").read_text()
            )
            cf.get_waiter("stack_create_complete").wait(StackName=bootstrap_stack)
        print(json.dumps(outputs(cf, bootstrap_stack), indent=2))
        return
    if args.command == "apply":
        reviewed = cf.describe_change_set(ChangeSetName=args.change_set)
        if reviewed["StackName"] != args.name:
            raise SystemExit("change set does not belong to requested stack")
        stack_status = cf.describe_stacks(StackName=args.name)["Stacks"][0]["StackStatus"]
        cf.execute_change_set(ChangeSetName=args.change_set)
        waiter = (
            "stack_create_complete"
            if stack_status == "REVIEW_IN_PROGRESS"
            else "stack_update_complete"
        )
        cf.get_waiter(waiter).wait(StackName=args.name)
        print(json.dumps(outputs(cf, args.name), indent=2))
        return
    if args.command == "status":
        result = outputs(cf, args.name)
        print(json.dumps(result, indent=2))
        ecs = session.client("ecs")
        services = ecs.describe_services(cluster=result["Cluster"], services=[result["Service"]])
        print(json.dumps(services["services"][0], indent=2, default=str))
        return
    base = outputs(cf, bootstrap_stack)
    if args.command == "upload-watchlist":
        from ctwatch.caa import issuer_mapping
        from ctwatch.matcher import Matcher

        body = args.file.read_bytes()
        matcher = Matcher(json.loads(body), str(ROOT / "data/confusables.txt"))
        issuer_mapping(json.loads(body))
        response = session.client("s3").put_object(
            Bucket=base["ConfigBucket"],
            Key="watchlist.json",
            Body=body,
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )
        print(
            json.dumps(
                {"ruleset_version": matcher.version, "s3_version": response.get("VersionId")}
            )
        )
        return
    if args.command == "build":
        registry = base["RepositoryUri"].split("/", 1)[0]
        token = session.client("ecr").get_authorization_token()["authorizationData"][0]
        import base64

        password = base64.b64decode(token["authorizationToken"]).decode().split(":", 1)[1]
        subprocess.run(
            ["docker", "login", "--username", "AWS", "--password-stdin", registry],
            input=password.encode(),
            check=True,
        )
        tag = f"build-{time.time_ns()}"
        image = base["RepositoryUri"] + ":" + tag
        platform = "linux/arm64" if args.architecture == "ARM64" else "linux/amd64"
        subprocess.run(
            [
                "docker",
                "buildx",
                "build",
                "--platform",
                platform,
                "--provenance=false",
                "--push",
                "-t",
                image,
                str(ROOT),
            ],
            check=True,
        )
        repository = base["RepositoryUri"].split("/", 1)[1]
        digest = session.client("ecr").describe_images(
            repositoryName=repository, imageIds=[{"imageTag": tag}]
        )["imageDetails"][0]["imageDigest"]
        print("Use immutable image: " + base["RepositoryUri"] + "@" + digest)
        return
    memory = args.memory
    valid = (
        (args.cpu == 4096 and 8192 <= memory <= 30720 and memory % 1024 == 0)
        or (args.cpu == 8192 and 16384 <= memory <= 61440 and memory % 4096 == 0)
        or (args.cpu == 16384 and 32768 <= memory <= 122880 and memory % 8192 == 0)
    )
    if not valid or args.workers < 1:
        raise SystemExit("invalid Fargate CPU/memory/worker configuration")
    if "@sha256:" not in args.image or not args.image.startswith(base["RepositoryUri"] + "@"):
        raise SystemExit("use the immutable digest from this bootstrap's build command")
    session.client("s3").head_object(Bucket=base["ConfigBucket"], Key="watchlist.json")
    parameters = {
        "ImageUri": args.image,
        "ConfigBucket": base["ConfigBucket"],
        "TaskCpu": str(args.cpu),
        "TaskMemory": str(memory),
        "Workers": str(args.workers),
        "Architecture": args.architecture,
        "DesiredCount": str(args.desired_count),
        "StartMode": "head",
        "PollSeconds": str(args.poll_seconds),
    }
    # Body is small enough for the direct API, verified by tests.
    template = (ROOT / "infra/service.json").read_text()
    name = "ctwatch-" + str(time.time_ns())
    deployed = exists(cf, args.name)
    if deployed:
        deployed = (
            cf.describe_stacks(StackName=args.name)["Stacks"][0]["StackStatus"]
            != "REVIEW_IN_PROGRESS"
        )
    response = cf.create_change_set(
        StackName=args.name,
        ChangeSetName=name,
        ChangeSetType="UPDATE" if deployed else "CREATE",
        Capabilities=["CAPABILITY_IAM"],
        TemplateBody=template,
        Parameters=[
            {"ParameterKey": key, "ParameterValue": value} for key, value in parameters.items()
        ],
    )
    cf.get_waiter("change_set_create_complete").wait(ChangeSetName=response["Id"])
    description = cf.describe_change_set(ChangeSetName=response["Id"])
    directory = ROOT / ".deploy"
    directory.mkdir(exist_ok=True)
    path = directory / (name + ".json")
    path.write_text(
        json.dumps(
            {
                "account": account,
                "region": args.region,
                "template_sha256": hashlib.sha256(template.encode()).hexdigest(),
                "parameters": parameters,
                "change_set": description,
            },
            indent=2,
            default=str,
        )
        + "\n"
    )
    print(json.dumps(description["Changes"], indent=2))
    print(f"Saved review: {path}\nApply explicitly: --change-set {response['Id']}")


if __name__ == "__main__":
    sys.exit(main())
