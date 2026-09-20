"""安装 python-dotenv 后，离线读取与脚本放在一起的演示配置。"""

import sys
from importlib import metadata
from pathlib import Path

from dotenv import dotenv_values


def main() -> None:
    print("\n=== 1. 运行解释器与实际依赖 ===")
    print("sys.executable:", sys.executable)
    print("python-dotenv 版本:", metadata.version("python-dotenv"))
    print("导入名: dotenv")

    print("\n=== 2. 读取本地 Agent 配置 ===")
    # 使用脚本位置定位配置：从仓库根目录或其他工作目录运行都读同一份文件。
    config_path: Path = Path(__file__).resolve().with_name("settings.env.example")
    if not config_path.is_file():
        raise FileNotFoundError(f"演示配置缺失: {config_path}")
    # 返回字典即可满足本例；无需 load_dotenv() 修改当前进程的环境变量。
    # 禁用变量插值，让输出只由这份示例文件决定，不读取外部变量来替换配置值。
    config: Any = dotenv_values(config_path, encoding="utf-8", interpolate=False)
    agent_name: Any = config.get("AGENT_NAME")
    status_url: Any = config.get("STATUS_URL")
    limit_text: Any = config.get("TOOL_LIMIT")
    if not agent_name or not status_url or not limit_text:
        raise ValueError("AGENT_NAME、STATUS_URL 和 TOOL_LIMIT 必须有值")

    # 配置解析得到字符串，不会自动把 '3' 变成整数，更不会校验业务上的正数范围。
    tool_limit = int(limit_text)
    if tool_limit <= 0:
        raise ValueError("TOOL_LIMIT 必须是正整数")
    print("配置文件:", config_path)
    print("助手名称:", agent_name)
    print("状态地址（仅展示，不发请求）:", status_url)
    print("原始 TOOL_LIMIT:", repr(limit_text), type(limit_text).__name__)
    print("转换后的工具数量上限:", tool_limit, type(tool_limit).__name__)
    print("本例只验证依赖安装与配置读取，不执行 HTTP、LLM 或并发工具调用。")


if __name__ == "__main__":
    main()
