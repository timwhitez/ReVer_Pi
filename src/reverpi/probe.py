from __future__ import annotations
import json
import secrets
from pathlib import Path
from .config import Provider, Budget
from .errors import LabError
from .ledger import Ledger
from .protocols import Message
from .transport import APIClient
from .util import atomic_write,canonical,process_lock,digest


async def probe(provider: Provider, budget: Budget, out: Path, *, transport=None):
    """Three-call live check: text + tool + signed/opaque reasoning replay."""
    out.mkdir(parents=True,exist_ok=True)
    with process_lock(out/"writer.lock"):
        ledger=Ledger(out/"ledger.sqlite",budget)
        # Durable nonce makes a restarted probe cache-identical.
        nonce_path=out/"nonce.txt"
        if not nonce_path.exists(): atomic_write(nonce_path,secrets.token_hex(8))
        nonce=nonce_path.read_text()
        report={"provider_sha":digest(provider.model_dump()),"protocol":provider.protocol,"requested_effort":provider.effort,
                "mapped_effort":provider.effort_map[provider.effort],"reasoning_style":provider.reasoning_style,
                "low_intensity_verified":False,"mock":provider.mock,"checks":{}}
        async with APIClient(provider,ledger,transport=transport) as client:
            try:
                text=await client.complete([Message("user","Reply with a JSON object containing ok=true. Do not use tools.")],op="probe:text",cell="probe")
                report["checks"]["nonempty_complete_response"]=bool(text.text.strip())
                if not report["checks"]["nonempty_complete_response"] or text.calls:
                    raise LabError("probe_text_contract", "Provider violated the text-only, no-tool probe contract")
                report["response_model"]=text.model
                tool={"name":"echo_nonce","description":"Return a nonce without external side effects.","parameters":{"type":"object","properties":{"nonce":{"type":"string"}},"required":["nonce"],"additionalProperties":False}}
                messages=[Message("system","First call echo_nonce exactly once with the supplied nonce. After its tool result, output ONLY JSON {\"nonce\":\"the nonce\"}."),Message("user","Nonce: "+nonce)]
                first=await client.complete(messages,tools=[tool],op="probe:tool",cell="probe")
                if len(first.calls)!=1 or first.calls[0]["name"]!="echo_nonce" or first.calls[0]["arguments"]!={"nonce":nonce}:
                    raise LabError("probe_tool_contract","Provider did not follow the probe tool contract; do not certify compatibility")
                report["checks"]["function_call"]=True
                messages += [first.message(),Message("tool",canonical({"nonce":nonce}),call_id=first.calls[0]["id"])]
                second=await client.complete(messages,tools=[tool],op="probe:replay",cell="probe")
                report["checks"]["tool_and_reasoning_replay"]=json.loads(second.text)=={"nonce":nonce}
                if not report["checks"]["tool_and_reasoning_replay"]:
                    raise LabError("probe_replay_contract","Round-trip probe answer did not match")
                report["accepted_request_parameters"]=True
                report["compatible_for_this_probe"]=True
            except (LabError,ValueError) as err:
                report["compatible_for_this_probe"]=False
                report["error"]=err.record() if isinstance(err,LabError) else {"kind":"probe_json"}
        report["cost"]=ledger.totals()
        report["caveat"]="Acceptance of a parameter does not prove internal reasoning intensity, future uptime, or all-context compatibility."
        atomic_write(out/"probe.json",canonical(report))
        return report
