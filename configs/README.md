# Configuration files

Every file here is an example or a default study profile. No file contains credentials; API keys are
read from the environment variable named by `api_key_env`.

| File | Purpose |
|---|---|
| `pilot.yaml` | Small default study: methods and budget used by the CLI's `--config` default |
| `smoke-all.yaml` | Broad method list for local smoke studies |
| `review.yaml` | Review-round study profile |
| `native-pilot.yaml` | Native (Harbor) pilot profile |
| `gateway.yaml` | Gateway-side study profile |
| `online-projection.example.yaml` | Online request-boundary projection (`mode: apply`) with the `split_v1` search/read tools, for a `mask` session with a `pi_original` baseline |
| `mock.chat.yaml`, `mock.responses.yaml` | Protocol-only mock providers used by the offline smoke run |
| `provider.chat.example.yaml`, `provider.responses.example.yaml`, `provider.thinking_only.example.yaml` | Provider templates for the three wire dialects |
| `provider.deepseek.example.yaml` | Worked example against a public API root |
| `flash/` | Compact JSON provider profiles, a demo inventory and a review scope |

Study-specific configuration from the research programme lives in `research/configs/`; deployment
profiles for private endpoints are deliberately not distributed.

To use a provider, copy an example, fill in your own values and export the key:

```bash
cp configs/provider.chat.example.yaml configs/provider.local.yaml
export REVER_API_KEY='...'
```
