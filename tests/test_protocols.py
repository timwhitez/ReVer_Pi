import copy,json
import pytest
from pydantic import ValidationError
from reverpi.config import Provider,Prices
from reverpi.errors import LabError,classify_http
from reverpi.protocols import Message,build_request,parse_completion,normalize_usage,validate_messages


def chat(content="ok",finish="stop",**kwargs):
    return {"model":"m","id":"r","choices":[{"message":{"role":"assistant","content":content,**kwargs},"finish_reason":finish}],"usage":{"prompt_tokens":20,"completion_tokens":5}}

def resp(items=None,status="completed"):
    return {"model":"m","status":status,"output":items or [{"type":"message","content":[{"type":"output_text","text":"ok"}]}],"usage":{"input_tokens":20,"output_tokens":5}}

@pytest.mark.parametrize("protocol",["chat_completions","responses"])
def test_low_field(provider,protocol):
    provider.protocol=protocol
    path,body=build_request(provider,[Message("user","hello")])
    assert body.get("reasoning_effort",body.get("reasoning",{}).get("effort"))=="low"
    assert "temperature" not in body and body["stream"] is False
    assert path in {"/responses","/chat/completions"}

@pytest.mark.parametrize("effort",["low","medium","high","xhigh","max"])
def test_effort_mapping(provider,effort):
    _,body=build_request(provider,[Message("user","x")],effort=effort)
    assert body["reasoning_effort"]==effort

@pytest.mark.parametrize("effort",["none","off","unsupported",""])
def test_bad_effort(provider,effort):
    with pytest.raises(LabError):build_request(provider,[Message("user","x")],effort=effort)

@pytest.mark.parametrize("maximum",[0,-1,True,4097,1.2])
def test_bad_output_cap(provider,maximum):
    with pytest.raises(LabError):build_request(provider,[Message("user","x")],max_output_tokens=maximum)

def test_thinking_toggle(provider):
    provider.reasoning_style="thinking"
    _,b=build_request(provider,[Message("user","x")]);assert b["thinking"]=={"type":"enabled"} and b["reasoning_effort"]=="low"
    provider.reasoning_style="thinking_only"
    _,b=build_request(provider,[Message("user","x")]);assert "reasoning_effort" not in b and b["thinking"]["type"]=="enabled"

@pytest.mark.parametrize("update",[
    {"base_url":"http://evil.example/v1"},{"base_url":"https://key:pass@example.com"},{"base_url":"https://example.com?key=abc"},
    {"effort_map":{"low":"off"}},{"extra_body":{"reasoning_effort":"none"}},{"extra_headers_env":{"Authorization":"TOKEN"}},
    {"protocol":"responses","reasoning_style":"thinking_only"},{"max_output_tokens":True},{"unknown":1}
])
def test_configuration_fail_closed(update):
    with pytest.raises(ValidationError):Provider(**update)

@pytest.mark.parametrize("messages",[
    [],[Message("tool","orphan",call_id="a")],
    [Message("assistant",calls=[{"id":"a","name":"f","arguments":{}}])],
    [Message("user",calls=[{"id":"a","name":"f","arguments":{}}])],
    [Message("assistant",calls=[{"id":"a","name":"f","arguments":{}}]),Message("user","gap")],
    [Message("assistant",calls=[{"id":"a","name":"f","arguments":{}}]),Message("tool","x",call_id="a"),Message("tool","x",call_id="a")],
])
def test_bad_tool_pairing(messages):
    with pytest.raises(LabError):validate_messages(messages)

def test_chat_reasoning_replay(provider):
    first=parse_completion(provider,chat(None,"tool_calls",reasoning_content="exact reasoning",tool_calls=[{"type":"function","id":"a","function":{"name":"f","arguments":"{\"x\":1}"}}]))
    _,body=build_request(provider,[Message("user","go"),first.message(),Message("tool","yes",call_id="a")])
    assert body["messages"][1]["reasoning_content"]=="exact reasoning"
    assert json.loads(body["messages"][1]["tool_calls"][0]["function"]["arguments"])=={"x":1}

def test_responses_encrypted_replay(provider):
    provider.protocol="responses"
    items=[{"type":"reasoning","id":"rs","encrypted_content":"OPAQUE"},{"type":"function_call","call_id":"a","name":"f","arguments":"{}"}]
    old=copy.deepcopy(items)
    result=parse_completion(provider,resp(items))
    _,body=build_request(provider,[Message("user","go"),result.message(),Message("tool","ok",call_id="a")])
    assert body["input"][1:3]==old and items==old
    assert body["input"][-1]["type"]=="function_call_output"
    assert body["store"] is False

@pytest.mark.parametrize("raw,kind",[(chat("partial","length"),"output_incomplete"),(chat("","stop",reasoning_content="only thinking"),"empty_completion"),
    (chat("","content_filter"),"refusal"),(chat("ok",None),"missing_terminal"),(chat("", "tool_calls"),"tool_pairing")])
def test_bad_chat_terminal(provider,raw,kind):
    with pytest.raises(LabError,match=kind):parse_completion(provider,raw)

@pytest.mark.parametrize("status",["incomplete","failed","in_progress",None])
def test_response_terminal(provider,status):
    provider.protocol="responses"
    with pytest.raises(LabError):parse_completion(provider,resp(status=status))

def test_unknown_hosted_tool(provider):
    provider.protocol="responses"
    with pytest.raises(LabError,match="unsupported_tool"):parse_completion(provider,resp([{"type":"web_search_call"}]))

def test_cross_protocol(provider):
    m=Message("assistant","text",response_items=[{"type":"message"}])
    with pytest.raises(LabError,match="cross_protocol"):build_request(provider,[m])
    provider.protocol="responses"
    with pytest.raises(LabError,match="cross_protocol"):build_request(provider,[Message("assistant","text",reasoning="chat-reasoning")])

@pytest.mark.parametrize("raw",[
    {"prompt_tokens":100,"completion_tokens":20,"prompt_tokens_details":{"cached_tokens":25},"completion_tokens_details":{"reasoning_tokens":15}},
    {"input_tokens":100,"output_tokens":20,"input_tokens_details":{"cached_tokens":25},"output_tokens_details":{"reasoning_tokens":15}},
])
def test_usage_no_double_count(raw):
    n,usd=normalize_usage(raw,Prices(configured=True,input_per_million=2,cached_input_per_million=1,output_per_million=4))
    assert n==120 and usd==pytest.approx((75*2+25+20*4)/1e6)

@pytest.mark.parametrize("usage",[None,{}, {"input_tokens":-1,"output_tokens":1},{"input_tokens":True,"output_tokens":1},{"input_tokens":1},
    {"input_tokens":1,"output_tokens":1,"input_tokens_details":{"cached_tokens":2}}])
def test_unknown_or_invalid_usage(usage):
    assert normalize_usage(usage,Prices())[0] is None

@pytest.mark.parametrize("status,kind,retry",[(401,"authentication",False),(403,"permission",False),(402,"quota",False),(429,"rate_limit",True),
    (408,"upstream_transient",True),(409,"upstream_transient",True),(500,"upstream_transient",True),(503,"upstream_transient",True),
    (404,"endpoint_or_model",False),(413,"context_overflow",False),(400,"invalid_request",False),(302,"redirect_rejected",False)])
def test_error_taxonomy(status,kind,retry):
    e=classify_http(status,{"error":{"message":"DO-NOT-ECHO-secret"}})
    assert e.kind==kind and e.retryable==retry and "secret" not in str(e)

def test_429_quota_not_rate_limit():
    assert classify_http(429,{"error":{"code":"insufficient_quota"}}).kind=="quota"
