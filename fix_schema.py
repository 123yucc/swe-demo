from flask import Flask, request, Response
import requests

app = Flask(__name__)

# 这里填入你真实的网关地址
GATEWAY_URL = "https://llmapigateway.org/v1"

def fix_schema(obj):
    """递归遍历请求中的 JSON，给所有缺失 properties 的 object 强行补上 properties: {}"""
    if isinstance(obj, dict):
        if obj.get('type') == 'object' and 'properties' not in obj:
            obj['properties'] = {}
        for k, v in obj.items():
            fix_schema(v)
    elif isinstance(obj, list):
        for item in obj:
            fix_schema(item)

@app.route('/<path:path>', methods=['POST', 'GET'])
def proxy(path):
    data = request.json
    # 核心：拦截并修复工具的 Schema 缺陷
    if data:
        fix_schema(data)
    
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ['host', 'content-length']}
    
    # 转发请求，必须开启 stream=True，否则 Claude Code 的打字机效果会卡死
    resp = requests.post(
        f"{GATEWAY_URL}/{path}",
        json=data,
        headers=headers,
        stream=True
    )
    
    def generate():
        for chunk in resp.iter_content(chunk_size=4096):
            if chunk:
                yield chunk

    excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
    headers = [(k, v) for k, v in resp.headers.items() if k.lower() not in excluded_headers]
    
    return Response(generate(), resp.status_code, headers)

if __name__ == '__main__':
    # 脚本运行在 4001 端口
    app.run(port=4001)

    """
    使用时需要同时打开三个 PowerShell 窗口（像流水线一样串起来）：

窗口 A： 运行 python fix_schema.py （启动修复脚本，监听 4001）

窗口 B： 运行 litellm --config config.yaml （启动 LiteLLM 翻译官，监听 4000）

窗口 C： 运行 claude --model claude-3-7-sonnet-20250219    --dangerously-skip-permissions
    """