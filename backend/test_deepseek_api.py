import os
import asyncio
from dotenv import load_dotenv
from openai import AsyncOpenAI

async def test_api():
    # 加载 .env 环境变量
    load_dotenv()

    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    model = os.environ.get("OPENAI_MODEL", "deepseek-chat")

    print(f"🔍 正在测试 API 连通性...")
    print(f"👉 Base URL: {base_url}")
    print(f"👉 Model: {model}")

    if not api_key or api_key == "sk-your_actual_api_key_here" or api_key == "sk-your_deepseek_key":
        print("❌ 错误: 未检测到有效的 API Key！请先在 backend/.env 文件中配置你的真实 Key。")
        return

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    try:
        # 发送一个极其简单的测试请求
        print("⏳ 正在向模型发送请求: '你好，请回复: API连接成功'...")
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "user", "content": "你好，请只回复: API连接成功"}
            ],
            max_tokens=20,
            temperature=0.1
        )
        reply = response.choices[0].message.content.strip()
        print("\n✅ API 连接测试成功！")
        print(f"🤖 模型回复: {reply}")
    except Exception as e:
        print("\n❌ API 连接测试失败！")
        print(f"错误信息: {str(e)}")

if __name__ == "__main__":
    asyncio.run(test_api())