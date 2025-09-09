from slack_sdk import WebClient
import os
client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
client.chat_postMessage(channel=os.environ["SLACK_CHANNEL_ID"], text="ポストテスト ✅")