import anthropic

client = anthropic.Anthropic()

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1000,
    messages=[
        {"role": "user", "content": "你好!用一句话介绍你自己,并祝贺我跑通了人生第一个 agent 程序。"}
    ]
)

print(response.content[0].text)