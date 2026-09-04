# Gateway extensions

## Feishu

`feishu.py` uses Feishu's long-connection mode, so local and Docker deployments
do not need a public callback URL.

1. In Feishu Open Platform, create an enterprise self-built app and enable the bot.
2. Grant `im:message.p2p_msg:readonly`, `im:message.group_at_msg:readonly`, and
   `im:message:send_as_bot` permissions.
3. Under **Events and Callbacks**, select long connection and subscribe to
   `im.message.receive_v1`.
4. Publish the app, then add the bot to the target group. Group messages must
   mention the bot; direct messages are accepted normally.
5. Put the app credentials and the model provider credentials in `.env`, then run:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[feishu]"
.\.venv\Scripts\run-agent-gateway.exe `
  --extension examples/gateway_extensions/feishu.py `
  --cwd .
```

Required variables are `FEISHU_APP_ID` and `FEISHU_APP_SECRET`. Set
`FEISHU_DOMAIN=https://open.larksuite.com` only when connecting to Lark rather
than Feishu.

The adapter maps each Feishu chat to one durable Gateway session and replies to
the triggering message. The SDK handles duplicate events, group mention policy,
reconnection, and message chunking.

Send `/new` in a direct chat, or mention the bot with `/new` in a group, to start
a fresh CodingSession for that Feishu chat. The previous context remains on disk
and durable Mem0 memory is not deleted.

## stdin JSONL

`stdin_jsonl.py` is a small local integration and smoke-test adapter. It reads
UTF-8 JSON objects from stdin and writes Gateway responses to stdout.
