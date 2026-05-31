#!/bin/bash
set -e
BASE_URL="http://localhost:8000"
USER="apitest_$(date +%s)"
PASS="test123456"

log() { echo -e "\n>>> $1"; }

log "1. 注册用户"
curl -s -X POST "${BASE_URL}/auth/register" \
  -H "Content-Type: application/json" \
  -d "{\"username\": \"${USER}\", \"password\": \"${PASS}\"}" | jq .

log "2. 登录获取Token"
TOKEN=$(curl -s -X POST "${BASE_URL}/auth/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=${USER}&password=${PASS}" | jq -r '.access_token')
echo "Token: $TOKEN"

log "3. 获取用户信息"
curl -s -X GET "${BASE_URL}/auth/me" -H "Authorization: Bearer ${TOKEN}" | jq .

log "4. 创建会话"
THREAD_RESP=$(curl -s -L -X POST "${BASE_URL}/threads" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"title": "API测试会话"}')

# 检查是否为有效 JSON
if ! echo "$THREAD_RESP" | jq empty 2>/dev/null; then
    echo "❌ 响应非 JSON:"
    echo "$THREAD_RESP"
    exit 1
fi

echo "$THREAD_RESP" | jq .
THREAD_ID=$(echo "$THREAD_RESP" | jq -r '.thread_id')
if [ -z "$THREAD_ID" ] || [ "$THREAD_ID" = "null" ]; then
    echo "❌ 未获取到 thread_id"
    exit 1
fi
echo "Thread ID: $THREAD_ID"

log "5. 发送聊天消息（流式，仅显示前 8 行）"
curl -s -X POST "${BASE_URL}/chat" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"你好\", \"session_id\": \"${THREAD_ID}\"}" \
  --no-buffer | head -n 8

log "6. 获取历史消息"
sleep 2
curl -s -X GET "${BASE_URL}/threads/${THREAD_ID}/messages" \
  -H "Authorization: Bearer ${TOKEN}" | jq .

log "7. 删除会话"
curl -s -X DELETE "${BASE_URL}/threads/${THREAD_ID}" \
  -H "Authorization: Bearer ${TOKEN}" | jq .

echo -e "\n✅ 所有测试通过"