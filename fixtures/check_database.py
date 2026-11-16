import os

import anthropic
from dotenv import dotenv_values


def describe_key(label, key_value):
    if key_value is None:
        print(f"{label}: not set")
        return
    has_edge_whitespace = key_value != key_value.strip()
    has_quote_characters = '"' in key_value or "'" in key_value
    print(f"{label}: length={len(key_value)}, "
          f"first 10 characters={key_value[:10]!r}, "
          f"edge_whitespace={has_edge_whitespace}, "
          f"quote_characters={has_quote_characters}")


environment_value = os.environ.get("ANTHROPIC_API_KEY")
env_file_value = dotenv_values().get("ANTHROPIC_API_KEY")

describe_key("process environment", environment_value)
describe_key(".env file", env_file_value)
print("the two are identical:", environment_value == env_file_value)

for label, key_value in (("process environment", environment_value),
                         (".env file", env_file_value)):
    if not key_value:
        continue
    anthropic_client = anthropic.Anthropic(api_key=key_value.strip())
    try:
        anthropic_client.models.list(limit=1)
        print(f"{label}: key accepted")
    except anthropic.AuthenticationError:
        print(f"{label}: key REJECTED")
