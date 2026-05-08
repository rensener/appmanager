#!/bin/bash
# App Manager 启动脚本
# 用法: ./run.sh

set -e
cd "$(dirname "$0")"

# 激活虚拟环境
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
else
    echo "错误: 未找到虚拟环境。请先创建: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

# 运行
python3 src/main.py "$@"
