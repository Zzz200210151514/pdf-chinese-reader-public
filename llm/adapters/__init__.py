"""Stdlib transports. Protocol references are recorded in llm/README.md."""
from dataclasses import dataclass
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, quote
from urllib.request import Request, build_opener, HTTPRedirectHandler


@dataclass
class ProviderResponse:
    text: object
    usage: dict | None = None
    retries: int | None = None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward authorization to a redirected endpoint.
        return None


def https_transport(url, headers, body, timeout):
    request = Request(url, data=json.dumps(body, ensure_ascii=False).encode('utf-8'), headers=headers, method='POST')
    with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
        return json.loads(response.read(16 * 1024 * 1024).decode('utf-8'))


class RemoteAdapter:
    def __init__(self, config, transport=None):
        self.config = dict(config)
        self.transport = transport or https_transport

    def __call__(self, job):
        cfg = {**self.config, **job.get('route_config', {})}
        provider, model = cfg.get('provider'), cfg.get('model')
        if provider not in ('openai', 'openai-compatible', 'qwen', 'local', 'anthropic', 'claude', 'gemini'):
            raise ValueError('Unsupported configured remote provider')
        if not isinstance(model, str) or not model.strip():
            raise ValueError('Remote provider requires an explicitly selected model')
        base = cfg.get('base_url')
        if not isinstance(base, str):
            raise ValueError('Remote provider requires an explicit base_url')
        parsed = urlsplit(base)
        local = provider == 'local' and parsed.hostname in ('localhost', '127.0.0.1', '::1')
        if parsed.scheme != 'https' and not (local and parsed.scheme == 'http'):
            raise ValueError('Remote base_url requires HTTPS (local loopback may use HTTP)')
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('base_url must have no credentials, query or fragment')
        if any(k in cfg for k in ('api_key', 'key', 'token', 'headers')):
            raise ValueError('Secrets and arbitrary headers are not accepted in config; use key_env')
        env_name = cfg.get('key_env')
        key = os.environ.get(env_name, '') if isinstance(env_name, str) else ''
        if not key and not local:
            raise ValueError('Configured key_env is missing from environment')
        headers = {'Content-Type': 'application/json'}
        payload = {k: job[k] for k in ('blocks', 'neighbor_context', 'terms')}
        user = json.dumps(payload, ensure_ascii=False)
        system = job['instructions']
        if provider in ('anthropic', 'claude'):
            url = base.rstrip('/') + '/messages'
            headers.update({'x-api-key': key, 'anthropic-version': '2023-06-01'})
            body = {'model': model, 'max_tokens': int(cfg.get('max_output_tokens', 8192)), 'system': system, 'messages': [{'role': 'user', 'content': user}]}
        elif provider == 'gemini':
            url = base.rstrip('/') + '/models/' + quote(model, safe='') + ':generateContent'
            headers['x-goog-api-key'] = key
            body = {'systemInstruction': {'parts': [{'text': system}]}, 'contents': [{'role': 'user', 'parts': [{'text': user}]}], 'generationConfig': {'responseMimeType': 'application/json'}}
        else:
            url = base.rstrip('/') + '/chat/completions'
            if key:
                headers['Authorization'] = 'Bearer ' + key
            body = {'model': model, 'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}], 'response_format': {'type': 'json_object'}}
        timeout = float(cfg.get('timeout', 60))
        retries = int(cfg.get('retries', 2))
        if not 0 < timeout <= 300 or not 0 <= retries <= 5:
            raise ValueError('timeout must be in (0,300], retries in [0,5]')
        for attempt in range(retries + 1):
            try:
                response = self.transport(url, headers, body, timeout)
                if provider in ('anthropic', 'claude'):
                    content = ''.join(part['text'] for part in response['content'] if part.get('type') == 'text')
                    usage = response.get('usage')
                elif provider == 'gemini':
                    content = ''.join(part.get('text', '') for part in response['candidates'][0]['content']['parts'])
                    usage = response.get('usageMetadata')
                else:
                    content = response['choices'][0]['message']['content']
                    usage = response.get('usage')
                return ProviderResponse(content, usage, attempt)
            except HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                if not retryable or attempt == retries:
                    raise RuntimeError(f'Provider request failed (HTTP {exc.code})') from None
            except (URLError, TimeoutError, OSError):
                if attempt == retries:
                    raise RuntimeError('Provider request failed (transport)') from None
            except (KeyError, TypeError, ValueError, IndexError):
                raise RuntimeError('Provider returned an invalid response envelope') from None
            time.sleep(min(2 ** attempt, 8))
