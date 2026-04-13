"""Quick smoke test for the cloud module."""
from localforge.cloud.client import _split_concatenated_json, _parse_stream_chunks

# Test 1: JSON splitter
raw = '{"a":1}{"b":2}{"c":{"nested":"val"}}'
chunks = _split_concatenated_json(raw)
assert len(chunks) == 3, f"Expected 3 chunks, got {len(chunks)}"
print(f"PASS: JSON splitter ({len(chunks)} chunks)")

# Test 2: Realistic API response
import json
resp1 = json.dumps({"model_response": {"role": "system", "content": "", "thinking": "Step 1"}, "error": None, "conversation_id": "abc"})
resp2 = json.dumps({"model_response": {"role": "system", "content": "Hello!", "thinking": ""}, "error": None, "conversation_id": "abc"})
resp3 = json.dumps({"model_response": {"role": "system", "content": " World", "thinking": ""}, "error": None, "conversation_id": "abc"})
raw_api = resp1 + resp2 + resp3

parsed = _parse_stream_chunks(raw_api)
assert len(parsed) == 3, f"Expected 3 parsed, got {len(parsed)}"

contents = [c["model_response"]["content"] for c in parsed]
full_content = "".join(contents)
assert full_content == "Hello! World", f"Got: {full_content!r}"
print(f"PASS: Stream parser (content={full_content!r})")

thinking = [c["model_response"]["thinking"] for c in parsed if c["model_response"]["thinking"]]
assert thinking == ["Step 1"], f"Got: {thinking}"
print(f"PASS: Thinking extraction")

conv_id = parsed[0]["conversation_id"]
assert conv_id == "abc"
print(f"PASS: Conversation ID: {conv_id}")

# Test 3: Auth header parser
from localforge.cloud.auth import parse_raw_headers, validate_headers

raw_headers = """POST /api/digital-assistant-backend/messages?regenerate=false HTTP/1.1
Accept: */*
Cookie: BEPSESSION=abc123test; OCWEBSESSIONID=xyz789
Host: operationcentre.ms.bell.ca
Origin: https://operationcentre.ms.bell.ca
Referer: https://operationcentre.ms.bell.ca/digital-assistant/
content-type: application/json
User-Agent: Mozilla/5.0"""

parsed_h = parse_raw_headers(raw_headers)
assert parsed_h["base_url"] == "https://operationcentre.ms.bell.ca"
assert "regenerate=false" in parsed_h["api_path"]
assert "Cookie" in parsed_h["headers"]
assert "BEPSESSION" in parsed_h["headers"]["Cookie"]
print(f"PASS: Header parser (base_url={parsed_h['base_url']})")

ok, msg = validate_headers(parsed_h)
assert ok, f"Validation failed: {msg}"
print(f"PASS: Header validation")

# Test 4: Session
from localforge.cloud.session import CloudChatSession
s = CloudChatSession(session_id="test")
s.add_user_message("hello")
s.add_assistant_message("hi there", thinking="thought about it")
assert len(s.messages) == 2
assert s.messages[1].thinking == "thought about it"
print("PASS: CloudChatSession")

# Test 5: CloudClient payload building
from localforge.cloud.client import CloudClient
auth = {"base_url": "https://example.com", "api_path": "/api/test", "headers": {"Cookie": "test=1"}}
c = CloudClient(auth)
payload = c._build_payload_from_messages(
    [{"role": "user", "content": "hello"}],
    system="Be helpful",
    temperature=0.5,
)
assert payload["system_purpose"] == "code"
assert len(payload["messages"]) == 1
assert "[SYSTEM INSTRUCTIONS" in payload["messages"][0]["content"]
assert "hello" in payload["messages"][0]["content"]
print("PASS: CloudClient payload building")

print("\nAll tests passed!")
