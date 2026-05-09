#!/usr/bin/env python3
"""统一通知：用飞书胖虎智能助手发送到胖虎助理群"""
import json, urllib.request, sys

def send(text):
    c = json.load(open("/home/ubuntu/feishu-sync/config.json"))
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": c["app_id"], "app_secret": c["app_secret"]}).encode(),
        headers={"Content-Type": "application/json"}
    )
    token = json.loads(urllib.request.urlopen(req).read())["tenant_access_token"]
    msg = {
        "receive_id": c["chat_id"],
        "msg_type": "text",
        "content": json.dumps({"text": text})
    }
    req2 = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        data=json.dumps(msg).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    )
    resp = json.loads(urllib.request.urlopen(req2).read())
    return resp.get("code") == 0

if __name__ == "__main__":
    text = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    if send(text):
        print("OK")
    else:
        print("FAIL")
