#!/usr/bin/env bash
# 飞书 MCP 启动脚本（供 .mcp.json 调用）
# 依赖两个环境变量（在 Claude Code 环境设置里配置，不要写进仓库）：
#   LARK_APP_ID     飞书自建应用的 App ID
#   LARK_APP_SECRET 飞书自建应用的 App Secret
set -e

if [ -z "$LARK_APP_ID" ] || [ -z "$LARK_APP_SECRET" ]; then
  echo "错误：未设置 LARK_APP_ID / LARK_APP_SECRET 环境变量" >&2
  exit 1
fi

# lark-mcp 的依赖 keytar 需要 libsecret，云端容器是全新的，缺了就装
if ! pkg-config --exists libsecret-1 2>/dev/null; then
  apt-get install -y libsecret-1-dev pkg-config >/dev/null 2>&1 || true
fi

# preset.base.default = 多维表格全套工具（列数据表/列字段/增改查记录）
exec npx -y @larksuiteoapi/lark-mcp@0.5.1 mcp \
  -a "$LARK_APP_ID" \
  -s "$LARK_APP_SECRET" \
  -t "preset.base.default" \
  -l zh
