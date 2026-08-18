# Custom proxy API profiles

Custom profiles use the same proxy-backed native-endpoint contract as curated
Inference Hub models. CAMC starts a loopback proxy but does not translate
request bodies between endpoint families.

```json
{
  "providers": {
    "my-provider": {
      "display_name": "My provider",
      "base_url": "https://api.example.com",
      "auth_key": "my_provider",
      "env_names": ["MY_PROVIDER_TOKEN"],
      "token_file": "~/.my_tokens.yaml",
      "catalog_path": "/v1/models"
    }
  },
  "apis": {
    "my-model": {
      "provider": "my-provider",
      "model": "vendor/my-model",
      "available_endpoints": ["messages"],
      "enabled": true,
      "allow_run": true
    }
  }
}
```

Use `token_key` only when the key inside `token_file` is not the provider's
`auth_key`. Claude profiles must advertise `messages`; Codex profiles must
advertise `responses`. Endpoint and URL suffix details are JSON-owned through
`tool_supported_endpoints` and `tool_base_url_suf`, not adapter TOML.

Validate with:

```text
camc api check
camc api list --all
camc run -t claude --api my-model
```

The curated profiles are the supported production path. A custom profile must
set `allow_run: true` explicitly and remains subject to native endpoint and
token validation.
