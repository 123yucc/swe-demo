#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试 Claude SDK LLM 调��（使用 Pro 订阅 OAuth session）。

运行方式:
    python scripts/test_llm_oauth.py
"""

import sys
import io
import asyncio
from pathlib import Path

# 设置 stdout 为 utf-8
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def get_claude_executable():
    """获取 Claude Code CLI 路径。"""
    import shutil
    import os

    # 1. 环境变量
    env_path = os.environ.get("CLAUDE_CODE_EXECUTABLE")
    if env_path and Path(env_path).exists():
        return env_path

    # 2. PATH
    found = shutil.which("claude")
    if found:
        return found

    # 3. Windows 常见路径
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    common_paths = [
        Path(local_appdata) / "Programs" / "Claude" / "claude.exe",
        Path.home() / "AppData" / "Local" / "Programs" / "Claude" / "claude.exe",
    ]
    for p in common_paths:
        if p.exists():
            return str(p)

    return None


async def test_simple_llm_call():
    """测试简单的 LLM 调用。"""
    print("="*60)
    print("Testing Claude SDK LLM Call (OAuth Session)")
    print("="*60)

    # 检查 SDK 是否可用
    try:
        from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
        print("[OK] Claude Agent SDK imported successfully")
    except ImportError as e:
        print(f"[FAIL] Claude Agent SDK not available: {e}")
        return False

    # 获取 Claude 可执行文件路径
    claude_exe = get_claude_executable()
    if claude_exe:
        print(f"[OK] Found Claude executable: {claude_exe}")
    else:
        print("[WARN] Claude executable not found, will try default")
        claude_exe = None

    # 获取项目目录
    project_cwd = str(project_root)
    print(f"[INFO] Project CWD: {project_cwd}")

    # 简单的测试 prompt
    test_prompt = "Please respond with exactly this text: success"

    print("\n[INFO] Attempting LLM call...")
    print(f"  - cli_path: {claude_exe}")
    print(f"  - cwd: {project_cwd}")
    print(f"  - permission_mode: bypassPermissions")

    try:
        async with ClaudeSDKClient(
            options=ClaudeAgentOptions(
                # 订阅认证配置 - 使用正确的参数名
                cli_path=claude_exe,
                cwd=project_cwd,
                setting_sources=["project"],
                permission_mode="bypassPermissions",
                # 简单配置
                thinking={"type": "disabled"},
                effort="low",
            )
        ) as client:
            # 发送查询
            await client.query(test_prompt)
            print("[OK] Query sent successfully")

            # 接收响应
            print("[INFO] Waiting for response...")
            response_text = None

            async for message in client.receive_response():
                msg_type = type(message).__name__
                print(f"[DEBUG] Received message type: {msg_type}")

                # 处理不同类型的消息
                if hasattr(message, 'content'):
                    if isinstance(message.content, str):
                        response_text = message.content
                        print(f"[OK] Response content (str): {response_text[:200]}...")
                    elif isinstance(message.content, list):
                        for block in message.content:
                            if hasattr(block, 'text'):
                                response_text = block.text
                                print(f"[OK] Response text (block): {response_text[:200]}...")
                                break

                # 检查 result
                if hasattr(message, 'result'):
                    print(f"[OK] Result: {message.result}")

            if response_text:
                print(f"\n[SUCCESS] LLM Response: {response_text}")
                return True
            else:
                print("\n[WARN] No text response received, but call completed")
                return True

    except Exception as e:
        print(f"\n[FAIL] LLM call failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_structured_output():
    """测试结构化输出。"""
    print("\n" + "="*60)
    print("Testing Structured Output")
    print("="*60)

    try:
        from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    except ImportError:
        print("[SKIP] SDK not available")
        return False

    claude_exe = get_claude_executable()
    project_cwd = str(project_root)

    # 简单的 JSON schema
    simple_schema = {
        "type": "object",
        "properties": {
            "greeting": {"type": "string"},
            "number": {"type": "number"}
        },
        "required": ["greeting", "number"]
    }

    test_prompt = "Return a JSON object with greeting='hello' and number=42. Only output the JSON object."

    print(f"[INFO] Attempting structured output call...")

    try:
        async with ClaudeSDKClient(
            options=ClaudeAgentOptions(
                cli_path=claude_exe,
                cwd=project_cwd,
                setting_sources=["project"],
                permission_mode="bypassPermissions",
                # 结构化输出
                output_format={"type": "json_schema", "schema": simple_schema},
                thinking={"type": "disabled"},
                effort="low",
            )
        ) as client:
            await client.query(test_prompt)
            print("[OK] Query sent")

            async for message in client.receive_response():
                msg_type = type(message).__name__
                print(f"[DEBUG] Message type: {msg_type}")

                # 检查结构化输出
                if hasattr(message, 'structured_output') and message.structured_output:
                    print(f"[SUCCESS] Structured output: {message.structured_output}")
                    return True

                # 也检查 AssistantMessage
                from claude_agent_sdk import AssistantMessage
                if isinstance(message, AssistantMessage):
                    if hasattr(message, 'structured_output') and message.structured_output:
                        print(f"[SUCCESS] Structured output from AssistantMessage: {message.structured_output}")
                        return True

            print("[WARN] No structured output received")
            return False

    except Exception as e:
        print(f"[FAIL] Structured output test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """运行所有测试。"""
    print("Claude SDK OAuth Session Test")
    print("="*60)

    # 检查环境
    import os
    print(f"\n[ENV] LOCALAPPDATA: {os.environ.get('LOCALAPPDATA', 'not set')}")
    print(f"[ENV] CLAUDE_CODE_EXECUTABLE: {os.environ.get('CLAUDE_CODE_EXECUTABLE', 'not set')}")

    # 检查 .claude 目录
    claude_dir = project_root / ".claude"
    if claude_dir.exists():
        print(f"[OK] .claude directory exists: {claude_dir}")
        # 列出内容
        for item in claude_dir.iterdir():
            print(f"  - {item.name}")
    else:
        print(f"[WARN] .claude directory not found: {claude_dir}")

    print()

    # 测试1: 简单调用
    result1 = await test_simple_llm_call()

    # 测试2: 结构化输出
    result2 = await test_structured_output()

    # 汇总
    print("\n" + "="*60)
    print("RESULTS")
    print("="*60)
    print(f"  Simple LLM call: {'PASS' if result1 else 'FAIL'}")
    print(f"  Structured output: {'PASS' if result2 else 'FAIL'}")

    return result1 or result2


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
