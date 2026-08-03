import time
import json
import openai
import logging
import hashlib
import os
from typing import Optional

logging.getLogger('httpx').setLevel(logging.ERROR)

### BEGIN CACHE

class HashKey:
    def __init__(self, info):
        self.cache_key = json.dumps(info, sort_keys=True)
        self.cache_hash = int(hashlib.sha256(self.cache_key.encode()).hexdigest()[:8], 16)

class NullCache:
    def __init__(self):
        pass

    def get(self, k: HashKey) -> Optional[object]:
        return None

    def put(self, k: HashKey, v: object):
        pass

llm_cache_chat = NullCache()

### END CACHE


def normalize_openai_base_url(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url += "/v1"
    return base_url

def send_request_azure(endpoint, api_key):
    def s(model, messages, tools, kwargs):
        max_tokens = 8192  # range: [1, 8192]
        data = {
            'model': model,
            'messages': messages,
            'max_tokens': max_tokens,
            'tools': tools,
            **kwargs,
        }

        if not tools: # Invalid 'tools': empty array. Expected an array with minimum length 1, but got an empty array instead.
            del data["tools"]

        if 'gpt-5-' in model:
            # Unsupported value: 'temperature' does not support 0 with this model. Only the default (1) value is supported.
            if 'temperature' in data:
                del data['temperature']

            # Unsupported parameter: 'stop' is not supported with this model.
            if 'stop' in data:
                del data['stop']

            data['reasoning_effort'] = 'low'

        hk = HashKey(data)

        res = llm_cache_chat.get(hk)
        if res:
            # print('cache hit')
            return res

        client = openai.AzureOpenAI(
            azure_endpoint=endpoint,
            api_version="2024-03-01-preview",
            api_key=api_key,
        )

        max_retries = 12
        retries = 0
        while retries < max_retries:
            try:
                completion = client.chat.completions.create(**data)
                if completion is None:
                    raise Exception("completion is None")

                if data.get('stream', False):
                    assert not data.get('tools', [])

                    resp_json = {
                        'choices': [{
                            'message': {
                                'role': 'assistant',
                                'content': '',
                                'refusal': None,
                                'annotations': None,
                                'audio': None,
                                'function_call': None,
                                'tool_calls': None,
                                'reasoning_content': '',
                            },
                            'finish_reason': None,
                            'index': 0,
                            'logprobs': None,
                        }],
                        'usage': {},
                    }

                    for ev in completion:
                        ev = ev.model_dump()
                        c = ev['choices']
                        if c:
                            assert len(c) == 1
                            c = c[0]
                            if c['finish_reason']:
                                resp_json['choices'][0]['finish_reason'] = c['finish_reason']
                            if c['delta'] and c['delta']['content']:
                                resp_json['choices'][0]['message']['content'] += c['delta']['content']
                        if ev.get('usage', None):
                            resp_json['usage'] = ev['usage']

                else:
                    resp_json = completion.model_dump()

                llm_cache_chat.put(hk, res)
                return resp_json
            except (openai.RateLimitError, openai.InternalServerError, openai.APITimeoutError, openai.APIConnectionError, openai.LengthFinishReasonError, openai.ContentFilterFinishReasonError) as e:
                print(f"An error occurred: {type(e)} {e}")
                if retries < max_retries:
                    time.sleep(2 ** retries)
                retries += 1
            except Exception as e: # (openai.APIStatusError, openai.BadRequestError)
                print(f"A fatal error occurred: {type(e)} {e}")
                raise e

        print(f"Maximum retries ({max_retries}) exceeded.")
        return None

    return s

def send_request_openai(base_url, api_key, request_model=None):
    def s(model, messages, tools, kwargs):
        request_model_name = request_model or model
        max_tokens = 8192  # range: [1, 8192]
        data = {
            'model': request_model_name,
            'messages': messages,
            'max_tokens': max_tokens,
            'tools': tools,
            **kwargs,
        }

        if not tools: # Invalid 'tools': empty array. Expected an array with minimum length 1, but got an empty array instead.
            del data["tools"]

        if 'gpt-5-' in request_model_name:
            # Unsupported value: 'temperature' does not support 0 with this model. Only the default (1) value is supported.
            if 'temperature' in data:
                del data['temperature']

            # Unsupported parameter: 'stop' is not supported with this model.
            if 'stop' in data:
                del data['stop']

            data['reasoning_effort'] = 'low'

        hk = HashKey(data)

        res = llm_cache_chat.get(hk)
        if res:
            # print('cache hit')
            return res

        client = openai.OpenAI(
            base_url=base_url,
            api_key=api_key,
        )

        max_retries = 12
        retries = 0
        while retries < max_retries:
            try:
                completion = client.chat.completions.create(**data)
                if completion is None:
                    raise Exception("completion is None")

                # if completion.choices[0].message.content == "":
                # raise Exception("completion.choices[0].message.content is empty")
                resp_json = completion.model_dump()

                llm_cache_chat.put(hk, res)
                return resp_json
            except Exception as e:
                print(f"An error occurred: {e}")
                if retries < max_retries:
                    time.sleep(2 ** retries)
                retries += 1

        print(f"Maximum retries ({max_retries}) exceeded.")
        return None

    return s

DEFAULT_BASE_URL = normalize_openai_base_url(
    os.environ.get('AGENTDIET_BASE_URL', 'https://base_url')
)
DEFAULT_API_KEY = os.environ.get('AGENTDIET_API_KEY', 'api_key')

UPSTREAMS_PER_MODEL = {
    'gemini-2.5-pro': send_request_openai(DEFAULT_BASE_URL, DEFAULT_API_KEY),
    'gemini-2.5-flash': send_request_openai(DEFAULT_BASE_URL, DEFAULT_API_KEY),
    'gpt-5-mini-2025-08-07': send_request_openai(DEFAULT_BASE_URL, DEFAULT_API_KEY),
    'claude4-sonnet': send_request_openai(DEFAULT_BASE_URL, DEFAULT_API_KEY, request_model='claude-sonnet-4-6'),
    'claude35-haiku': send_request_openai(DEFAULT_BASE_URL, DEFAULT_API_KEY, request_model='claude-3-5-haiku'),
    'deepseek-chat': send_request_openai(DEFAULT_BASE_URL, DEFAULT_API_KEY),
    'qwen3-235b-a22b-instruct-2507': send_request_openai(DEFAULT_BASE_URL, DEFAULT_API_KEY),
}

def get_llm_response(model: str, messages, tools, kwargs):
    upstream = UPSTREAMS_PER_MODEL[model]
    # time.sleep(10)
    decoded_answer = []
    finish_reason = []
    assistant_response = upstream(model, messages, tools, kwargs)
    if not assistant_response:
        raise RuntimeError('no response from api')
    # print(assistant_response)
    for choice in assistant_response["choices"]:
        decoded_answer.append(choice["message"])
        finish_reason.append(choice["finish_reason"])
    return decoded_answer, finish_reason, assistant_response["usage"]

if __name__ == "__main__":
    print(get_llm_response(
        "gpt-5-2025-08-07",
        [
                {"role": "system", "content": "You respond to what the user says."},
                {"role": "user", "content": "hello"},
        ],
        [],
        dict(temperature = 0.0, n = 1, stream=False),
    ))
