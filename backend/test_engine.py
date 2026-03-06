import os
from pathlib import Path
from app.engine.compiler import ExtremeCompiler

def test():
    # 路径配置
    workspace_dir = Path("/Users/zengchanghuan/Desktop/workspace/epub-factory")
    input_file = workspace_dir / "別把你的錢留到死：懂得花錢，是最好的投資——理想人生的9大財務思維.epub"
    output_file = workspace_dir / "backend" / "test_output.epub"
    
    if not input_file.exists():
        print(f"File not found: {input_file}")
        return

    print("--- Testing ExtremeCompiler ---")
    compiler = ExtremeCompiler(
        input_path=str(input_file),
        output_path=str(output_file),
        output_mode="simplified"
    )
    success = compiler.run()
    if success:
        print("✅ Test passed! Output file generated.")
    else:
        print("❌ Test failed!")

if __name__ == "__main__":
    test()