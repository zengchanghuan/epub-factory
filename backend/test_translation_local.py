import os
from pathlib import Path
from app.engine.compiler import ExtremeCompiler

# 模拟环境变量 (如果你有真实的 API_KEY，可以在终端 export 之后再跑，不要写在这里)
# os.environ["OPENAI_API_KEY"] = "sk-..."
# os.environ["OPENAI_BASE_URL"] = "https://api.deepseek.com/v1" # DeepSeek 示例
# os.environ["OPENAI_MODEL"] = "deepseek-chat"

def test():
    # 路径配置
    workspace_dir = Path("/Users/zengchanghuan/Desktop/workspace/epub-factory")
    # 我们最好找一本短小的英文 EPUB 来做测试
    # 此处假设用户在 input 里放了一个 test_en.epub，如果不存在则提示
    input_file = workspace_dir / "backend" / "test_en.epub"
    output_file = workspace_dir / "backend" / "test_en_translated.epub"
    
    if not input_file.exists():
        print(f"❌ Test file not found: {input_file}")
        print("💡 请准备一本英文 EPUB 文件，重命名为 test_en.epub 放到 backend/ 目录下。")
        print("💡 然后在终端执行: export OPENAI_API_KEY='你的KEY' && .venv/bin/python test_translation_local.py")
        return

    print("--- Testing ExtremeCompiler AI Translation ---")
    compiler = ExtremeCompiler(
        input_path=str(input_file),
        output_path=str(output_file),
        output_mode="simplified",
        enable_translation=True,    # 开启 AI 翻译
        target_lang="zh-CN"         # 翻译为简体中文
    )
    success = compiler.run()
    if success:
        print(f"✅ Test passed! Output file generated at {output_file}")
    else:
        print("❌ Test failed!")

if __name__ == "__main__":
    test()