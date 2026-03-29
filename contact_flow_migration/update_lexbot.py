"""
Bulk update Lex V2 bot references inside Amazon Connect contact flows.

Purpose:
When contact flows are migrated between Amazon Connect instances or environments,
the original Lex bot alias ARN may no longer be valid. This script replaces the
old Lex bot configuration with the target bot configuration in the current environment.

What this script does:
1. Read contact flows from the target Amazon Connect instance
2. Filter flows by a name keyword, such as CHAT or EMAIL
3. Read target Lex bot and alias information from Amazon Lex V2
4. Locate actions of type:
   - ConnectParticipantWithLexBot
5. Replace the runtime Lex alias ARN in:
   - Actions[*].Parameters.LexV2Bot.AliasArn
6. Update related metadata fields used by the Amazon Connect UI
7. Save the updated flow content back to Amazon Connect

Notes:
- AliasArn is the actual runtime field used by the flow
- Metadata fields are mainly used by the Connect flow editor UI
- The target Lex bot alias must already be associated with the target Amazon Connect instance
- This script only updates flows matching the specified name keyword
- BOT_MAPPING is the main mapping standard used to resolve old Lex bots to new target Lex bots.
  Update this section before use based on your own environment and bot naming standard.
"""

import json
import os
import boto3

# =========================
# Basic Config
# =========================
CONNECT_REGION = os.environ["CONNECT_REGION"]
LEX_REGION = os.environ["LEX_REGION"]
ACCOUNT_ID = os.environ["ACCOUNT_ID"]
INSTANCE_ID = os.environ["INSTANCE_ID"]

# Decide which flows to process, for example CHAT / EMAIL
FLOW_NAME_KEYWORD = os.environ["FLOW_NAME_KEYWORD"]

# Target bot alias name, for example Production
TARGET_BOT_ALIAS_NAME = os.environ["TARGET_BOT_ALIAS_NAME"]

# Main mapping used to resolve old Lex bot names to target Lex bot names.
# Replace this with your own mapping before use.
BOT_MAPPING = {
    "SourceBotA": "TargetBotA",
    "SourceBotB": "TargetBotB",
    "SourceBotC": "TargetBotC"
}

connect = boto3.client("connect", region_name=CONNECT_REGION)
lex = boto3.client("lexv2-models", region_name=LEX_REGION)


# =========================
# Helpers: Lex
# =========================
def get_bot_id_by_name(bot_name: str) -> str:
    resp = lex.list_bots(maxResults=1000)

    for bot in resp.get("botSummaries", []):
        if bot.get("botName") == bot_name:
            return bot["botId"]

    raise ValueError(f"Bot not found in Lex: {bot_name}")


def get_alias_id_by_name(bot_id: str, alias_name: str) -> str:
    resp = lex.list_bot_aliases(
        botId=bot_id,
        maxResults=1000
    )

    for alias in resp.get("botAliasSummaries", []):
        if alias.get("botAliasName") == alias_name:
            return alias["botAliasId"]

    raise ValueError(
        f"Alias '{alias_name}' not found for botId={bot_id}"
    )


def get_target_bot_info(bot_name: str) -> dict:
    bot_id = get_bot_id_by_name(bot_name)
    alias_id = get_alias_id_by_name(bot_id, TARGET_BOT_ALIAS_NAME)
    alias_arn = f"arn:aws:lex:{LEX_REGION}:{ACCOUNT_ID}:bot-alias/{bot_id}/{alias_id}"

    return {
        "botName": bot_name,
        "botId": bot_id,
        "aliasName": TARGET_BOT_ALIAS_NAME,
        "aliasId": alias_id,
        "aliasArn": alias_arn
    }


def build_target_bot_info_map(bot_mapping: dict) -> dict:
    result = {}

    for old_bot_name, new_bot_name in bot_mapping.items():
        info = get_target_bot_info(new_bot_name)
        result[old_bot_name] = info
        print(f"Resolved target bot: {old_bot_name} -> {info}")

    return result


# =========================
# Helpers: Connect Flow
# =========================
def list_contact_flows(instance_id: str) -> list:
    resp = connect.list_contact_flows(
        InstanceId=instance_id,
        ContactFlowTypes=["CONTACT_FLOW"],
        MaxResults=1000
    )
    return resp.get("ContactFlowSummaryList", [])


def get_flow_detail(instance_id: str, flow_id: str) -> dict:
    return connect.describe_contact_flow(
        InstanceId=instance_id,
        ContactFlowId=flow_id
    )["ContactFlow"]


# =========================
# Main Logic
# =========================
def lambda_handler(event, context):
    target_bot_info_map = build_target_bot_info_map(BOT_MAPPING)
    flows = list_contact_flows(INSTANCE_ID)

    updated = []
    skipped = []
    failed = []

    for flow in flows:
        flow_name = flow["Name"]

        # Only process flows containing the target keyword
        if FLOW_NAME_KEYWORD.upper() not in flow_name.upper():
            continue

        try:
            detail = get_flow_detail(INSTANCE_ID, flow["Id"])
            content_obj = json.loads(detail["Content"])
            changed = False

            action_metadata = content_obj.get("Metadata", {}).get("ActionMetadata", {})

            for action in content_obj.get("Actions", []):
                if action.get("Type") != "ConnectParticipantWithLexBot":
                    continue

                action_id = action.get("Identifier")
                params = action.get("Parameters", {})
                lex_bot = params.get("LexV2Bot", {})
                current_alias_arn = lex_bot.get("AliasArn", "")

                meta = action_metadata.get(action_id, {})
                old_bot_name = meta.get("lexV2BotName")

                if not old_bot_name:
                    continue

                if old_bot_name not in target_bot_info_map:
                    continue

                new_bot_info = target_bot_info_map[old_bot_name]

                # 1. Runtime
                if current_alias_arn != new_bot_info["aliasArn"]:
                    print(
                        f"[{flow_name}] Fixing Lex AliasArn: "
                        f"{current_alias_arn} -> {new_bot_info['aliasArn']}"
                    )
                    lex_bot["AliasArn"] = new_bot_info["aliasArn"]
                    changed = True

                # 2. Metadata
                alias_meta = (
                    meta.get("parameters", {})
                        .get("LexV2Bot", {})
                        .get("AliasArn", {})
                )

                if alias_meta.get("displayName") != new_bot_info["aliasName"]:
                    print(
                        f"[{flow_name}] Fixing displayName: "
                        f"{alias_meta.get('displayName')} -> {new_bot_info['aliasName']}"
                    )
                    alias_meta["displayName"] = new_bot_info["aliasName"]
                    changed = True

                if alias_meta.get("lexV2BotName") != new_bot_info["botName"]:
                    alias_meta["lexV2BotName"] = new_bot_info["botName"]
                    changed = True

                if meta.get("lexV2BotName") != new_bot_info["botName"]:
                    meta["lexV2BotName"] = new_bot_info["botName"]
                    changed = True

                if meta.get("lexV2BotAliasName") != new_bot_info["aliasName"]:
                    meta["lexV2BotAliasName"] = new_bot_info["aliasName"]
                    changed = True

            if not changed:
                skipped.append(flow_name)
                continue

            connect.update_contact_flow_content(
                InstanceId=INSTANCE_ID,
                ContactFlowId=flow["Id"],
                Content=json.dumps(content_obj)
            )

            print(f"[{flow_name}] UPDATED")
            updated.append(flow_name)

        except Exception as e:
            print(f"[{flow_name}] FAILED: {str(e)}")
            failed.append({
                "flow_name": flow_name,
                "error": str(e)
            })

    return {
        "flow_name_keyword": FLOW_NAME_KEYWORD,
        "updated_count": len(updated),
        "updated_flows": updated,
        "skipped_count": len(skipped),
        "skipped_flows": skipped,
        "failed_count": len(failed),
        "failed_flows": failed
    }