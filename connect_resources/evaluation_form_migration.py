"""
ENGLISH DESCRIPTION
-------------------
This script is used to migrate Amazon Connect Evaluation Forms from a source instance
to a target instance, including cross-region migration.

The script performs the following steps:

1. Connects to the source Amazon Connect instance and the target Amazon Connect instance.
2. Reads all Evaluation Forms from the source instance.
3. Reads all Evaluation Forms from the target instance.
4. Compares form titles between source and target.
5. Skips any form whose title already exists in the target instance.
6. Uses describe_evaluation_form to retrieve the full definition of each source form.
7. Recreates the form in the target instance using create_evaluation_form.
8. Preserves the following properties when available:
   - Title
   - Description
   - Items
   - ScoringStrategy
9. Returns migration statistics, including:
   - copied forms
   - skipped forms
   - failed forms

This script is useful for:
- migrating evaluation forms between DEV / UAT / PROD environments
- cross-region Amazon Connect configuration migration
- reducing manual recreation effort
- preventing duplicate forms by checking title existence in the target instance

Note:
Pagination is intentionally omitted because the number of evaluation forms in this environment
is expected to remain well below 100.


中文说明
--------
该脚本用于在 Amazon Connect 的 source instance 和 target instance 之间迁移
Evaluation Form（质检评分表），并支持跨区域迁移。

脚本执行逻辑如下：

1. 连接 source Amazon Connect instance 和 target Amazon Connect instance。
2. 读取 source instance 中的所有 Evaluation Form。
3. 读取 target instance 中的所有 Evaluation Form。
4. 根据表单 Title 比较 source 和 target 中的表单。
5. 如果 target instance 中已存在同名表单，则自动跳过，避免重复创建。
6. 使用 describe_evaluation_form 读取 source 表单的完整定义。
7. 使用 create_evaluation_form 在 target instance 中重新创建表单。
8. 在可用的情况下保留以下配置：
   - Title
   - Description
   - Items
   - ScoringStrategy
9. 返回迁移结果统计，包括：
   - copied（成功迁移）
   - skipped（已存在同名表单，跳过）
   - failed（迁移失败）

该脚本适用于：
- DEV / UAT / PROD 环境之间的 Evaluation Form 迁移
- Amazon Connect 跨区域配置迁移
- 减少手动重建表单的工作量
- 通过检查 target instance 中是否已存在同名 Title，防止重复创建

说明：
本脚本未实现分页，因为当前环境中的 Evaluation Form 数量预期远小于 100。
"""


import json
import boto3


def lambda_handler(event, context):
    # Hardcoded config
    source_region = "us-east-1"
    target_region = "ca-central-1"
    source_instance_id = "391138ba-7b4d-4b69-ab9e-a0669b698748"
    target_instance_id = "3f65a0d6-e8ed-422c-a4bf-8b88c7804453"

    source_connect = boto3.client("connect", region_name=source_region)
    target_connect = boto3.client("connect", region_name=target_region)

    copied = []
    skipped = []
    failed = []

    # 1. Read all evaluation forms from source instance
    source_resp = source_connect.list_evaluation_forms(
        InstanceId=source_instance_id,
        MaxResults=100
    )
    source_forms = source_resp.get("EvaluationFormSummaryList", [])

    # 2. Read all evaluation forms from target instance
    target_resp = target_connect.list_evaluation_forms(
        InstanceId=target_instance_id,
        MaxResults=100
    )
    target_forms = target_resp.get("EvaluationFormSummaryList", [])

    # 3. Build target title set for duplicate check
    target_titles = {
        form["Title"]
        for form in target_forms
        if form.get("Title")
    }

    print(f"Source forms count: {len(source_forms)}")
    print(f"Target forms count: {len(target_forms)}")

    for source_form in source_forms:
        form_id = source_form["EvaluationFormId"]
        title = source_form["Title"]

        try:
            # Skip if target already has same title
            if title in target_titles:
                skipped.append({
                    "source_form_id": form_id,
                    "title": title,
                    "reason": "Same title already exists in target instance"
                })
                print(f"Skipped: {title} | same title exists in target")
                continue

            # Read full form definition from source
            resp = source_connect.describe_evaluation_form(
                InstanceId=source_instance_id,
                EvaluationFormId=form_id
            )
            form = resp["EvaluationForm"]

            # Create form in target
            create_args = {
                "InstanceId": target_instance_id,
                "Title": form["Title"],
                "Description": form.get("Description", ""),
                "Items": form["Items"]
            }

            if "ScoringStrategy" in form and form["ScoringStrategy"] is not None:
                create_args["ScoringStrategy"] = form["ScoringStrategy"]

            out = target_connect.create_evaluation_form(**create_args)

            copied.append({
                "source_form_id": form_id,
                "target_form_id": out["EvaluationFormId"],
                "title": form["Title"]
            })

            print(f"Copied: {form['Title']} | {form_id} -> {out['EvaluationFormId']}")

        except Exception as e:
            failed.append({
                "source_form_id": form_id,
                "title": title,
                "error": str(e)
            })
            print(f"Failed: {title} | {form_id} | {str(e)}")

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "copied_count": len(copied),
                "copied": copied,
                "skipped_count": len(skipped),
                "skipped": skipped,
                "failed_count": len(failed),
                "failed": failed
            },
            ensure_ascii=False
        )
    }