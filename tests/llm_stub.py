"""Offline HTTP model stand-in used only for browser acceptance, never by production code."""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 200000:
            self.send_error(413)
            return
        payload = json.loads(self.rfile.read(length))
        if self.headers.get("Authorization") == "Bearer rejected-key":
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"offline rejected credential"}}')
            return
        messages = payload.get("messages", [])
        if messages and messages[-1].get("role") == "tool":
            result = json.loads(messages[-1]["content"])
            items = result.get("items", [])
            enabled = sum(item.get("status") == 1 for item in items)
            content = (
                f"已查询这个平台的渠道快照：共 {len(items)} 个渠道，{enabled} 个处于启用状态。"
                "备用渠道为手动禁用，不应直接判定为故障。以上来自最近一次采集，不代表实时探测。"
                if items
                else "已完成当前平台工具查询，结果已返回。"
            )
            message = {"role": "assistant", "content": content}
        elif not payload.get("tools"):
            message = {"role": "assistant", "content": "已连接离线测试模型"}
        else:
            tool_names = [tool.get("function", {}).get("name") for tool in payload["tools"]]
            selected = "get_capabilities" if tool_names == ["get_capabilities"] else "get_channels"
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "offline-call",
                        "type": "function",
                        "function": {"name": selected, "arguments": "{}"},
                    }
                ],
            }
        response = json.dumps({"choices": [{"message": message}]}, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
