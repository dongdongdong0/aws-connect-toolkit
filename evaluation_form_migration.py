import json
import boto3

connect = boto3.client("connect")

def lambda_handler(event, context):
    """
    Event:
    {
      "source_instance_id": "xxx",
      "target_instance_id": "yyy",
      "evaluation_form_ids": ["id1","id2","id3","id4","id5"]
    }
    """
    source_instance_id = event["source_instance_id"]
    target_instance_id = event["target_instance_id"]
    form_ids = event["evaluation_form_ids"]

    copied = []

    for form_id in form_ids:
        # 1) get form definition from source
        resp = connect.describe_evaluation_form(
            InstanceId=source_instance_id,
            EvaluationFormId=form_id
        )
        form = resp["EvaluationForm"]

        # 2) create same form in target
        create_args = {
            "InstanceId": target_instance_id,
            "Title": form["Title"],
            "Description": form.get("Description", ""),
            "Items": form["Items"],
        }

        # ScoringStrategy may exist depending on your form settings
        if "ScoringStrategy" in form and form["ScoringStrategy"] is not None:
            create_args["ScoringStrategy"] = form["ScoringStrategy"]

        out = connect.create_evaluation_form(**create_args)

        copied.append({
            "source_form_id": form_id,
            "target_form_id": out["EvaluationFormId"],
            "title": form["Title"]
        })

    return {
        "statusCode": 200,
        "body": json.dumps({"copied": copied}, ensure_ascii=False)
    }