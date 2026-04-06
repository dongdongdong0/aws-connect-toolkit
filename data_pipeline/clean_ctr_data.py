import base64
import json
import re
import unicodedata



# This logic standardizes incoming contact attributes before storing them in S3.

# It performs:
# - **Key normalization**: converts keys to lowercase, removes special characters, and enforces a consistent snake_case format
# - **Value normalization**:
#   - normalizes quotes and unicode characters
#   - removes control / invalid characters that may break Athena queries
#   - trims whitespace
#   - converts empty or null-like values (e.g., "", "null", "n/a") to `null`

# The goal is to ensure:
# - consistent schema for Glue/Athena
# - stable query behavior (avoiding `HIVE_BAD_DATA` errors)
# - cleaner downstream analytics in tools like Power BI

# The cleaned result is stored in `attributes_clean`.

def normalize_quotes(text: str) -> str:
    replacements = {
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "＂": '"',
        "＇": "'",
        "：": ":",
        "，": ",",
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "　": " ",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def normalize_key(key: str) -> str:
    if key is None:
        return ""

    key = str(key)
    key = normalize_quotes(key)
    key = unicodedata.normalize("NFKC", key)
    key = key.strip().lower()
    key = re.sub(r"[^a-z0-9]+", "_", key)
    key = re.sub(r"_+", "_", key)
    key = key.strip("_")
    return key


def normalize_value(value):
    if isinstance(value, str):
        value = normalize_quotes(value).strip()
        return None if value == "" else value
    return value


def lambda_handler(event, context):
    output = []

    for record in event["records"]:
        try:
            payload = base64.b64decode(record["data"]).decode("utf-8")
            data = json.loads(payload)

            raw_attrs = {}

            if isinstance(data.get("Attributes"), dict):
                raw_attrs.update(data["Attributes"])

            if isinstance(data.get("attributes"), dict):
                raw_attrs.update(data["attributes"])

            cleaned_attrs = {}

            for raw_key, raw_value in raw_attrs.items():
                new_key = normalize_key(raw_key)
                if not new_key:
                    continue

                new_value = normalize_value(raw_value)

                if new_key not in cleaned_attrs:
                    cleaned_attrs[new_key] = new_value
                else:
                    if cleaned_attrs[new_key] is None and new_value is not None:
                        cleaned_attrs[new_key] = new_value

            data["attributes_clean"] = cleaned_attrs

            new_data = base64.b64encode(
                json.dumps(data, ensure_ascii=False).encode("utf-8")
            ).decode("utf-8")

            output.append({
                "recordId": record["recordId"],
                "result": "Ok",
                "data": new_data
            })

        except Exception as e:
            print(f"Error processing record {record.get('recordId')}: {e}")
            output.append({
                "recordId": record["recordId"],
                "result": "ProcessingFailed",
                "data": record["data"]
            })

    return {"records": output}